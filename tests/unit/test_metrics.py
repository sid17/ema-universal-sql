"""Unit tests for the Prometheus side of the observability seam.

Split from `test_observability.py` when it crossed LAW 1's 400-line decompose
threshold. That file now covers tracing — spans, exporters, `trace_id`
propagation; this one covers the registry, the three collectors we own, and the
one route that renders them.

Infra-free: no Docker, no network. `REGISTRY` is process-wide and shared with
every other test in the session, so every assertion here is a **delta** rather
than an absolute — an absolute would pass or fail on collection order.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY, Histogram, generate_latest

from src.gateway.routes import query
from src.models.errors import InvalidQueryError
from src.observability.metrics import (
    CONNECTOR_FETCH_DURATION_NAME,
    QUERY_DURATION_NAME,
    RATE_LIMIT_REMAINING_NAME,
    _get_or_create,
    instrument_app,
    observe_connector_fetch,
    observe_query,
    query_duration_seconds,
    rate_limit_remaining,
    render_metrics,
    set_rate_limit_remaining,
)


def sample(name: str, **labels: str) -> float:
    """The current value of one sample, or 0.0 before anything is recorded.

    Read through `REGISTRY.get_sample_value` rather than the collector's private
    `_value`, because a histogram's count is a derived sample rather than an
    attribute — and because this is the same path a real scrape takes.
    """
    return REGISTRY.get_sample_value(name, labels or None) or 0.0


def test_rate_limit_remaining_renders_with_both_labels() -> None:
    rate_limit_remaining.labels(connector="github", tenant="tenant_acme").set(4870)

    body, content_type = render_metrics()
    assert content_type.startswith("text/plain")
    assert b'rate_limit_remaining{connector="github",tenant="tenant_acme"} 4870.0' in body


def test_golden_signals_land_in_the_same_registry_as_our_gauge() -> None:
    """ADR-015's single assumption, asserted end to end.

    `.instrument(app)` must feed `prometheus_client`'s shared `REGISTRY`, so one
    scrape carries both the golden signals and our gauge. If a future version of
    prometheus-fastapi-instrumentator splits the registry, this fails loudly
    instead of silently halving `/metrics`.
    """
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"status": "ok"}

    instrument_app(app)
    rate_limit_remaining.labels(connector="jira", tenant="tenant_globex").set(12)

    with TestClient(app) as client:
        assert client.get("/ping").status_code == 200

    scrape = generate_latest(REGISTRY)
    assert b"http_request_duration_seconds" in scrape
    assert b"http_requests_total" in scrape
    assert RATE_LIMIT_REMAINING_NAME.encode() in scrape
    assert b'rate_limit_remaining{connector="jira",tenant="tenant_globex"} 12.0' in scrape


# --- Phase 4: the two histograms, and the route's own timing ---------------


def test_query_duration_counts_a_request() -> None:
    before = sample(f"{QUERY_DURATION_NAME}_count")

    observe_query(0.25)

    assert sample(f"{QUERY_DURATION_NAME}_count") == before + 1
    assert sample(f"{QUERY_DURATION_NAME}_sum") >= 0.25


def test_connector_fetch_duration_is_recorded_per_connector() -> None:
    """The label is the whole point: a single aggregate cannot say "Jira was slow".

    Asserted as a **delta** rather than an absolute, because the registry is
    process-wide and other tests in this session record against the same two
    labels. An absolute assertion here would pass or fail on collection order,
    which is the definition of a flaky suite.
    """
    jira_before = sample(f"{CONNECTOR_FETCH_DURATION_NAME}_sum", connector="jira")
    github_before = sample(f"{CONNECTOR_FETCH_DURATION_NAME}_sum", connector="github")
    count_before = sample(f"{CONNECTOR_FETCH_DURATION_NAME}_count", connector="jira")

    observe_connector_fetch("jira", 0.18)
    observe_connector_fetch("github", 0.04)

    assert sample(f"{CONNECTOR_FETCH_DURATION_NAME}_count", connector="jira") == count_before + 1
    jira_delta = sample(f"{CONNECTOR_FETCH_DURATION_NAME}_sum", connector="jira") - jira_before
    github_delta = (
        sample(f"{CONNECTOR_FETCH_DURATION_NAME}_sum", connector="github") - github_before
    )
    assert jira_delta == pytest.approx(0.18)
    assert github_delta == pytest.approx(0.04)


def test_both_histograms_reach_a_scrape() -> None:
    observe_query(0.01)
    observe_connector_fetch("github", 0.01)

    body, _ = render_metrics()

    assert f"{QUERY_DURATION_NAME}_bucket".encode() in body
    assert f'{CONNECTOR_FETCH_DURATION_NAME}_count{{connector="github"}}'.encode() in body


def test_set_rate_limit_remaining_publishes_what_the_envelope_reports() -> None:
    """ADR-043: the gauge and `rate_limit_status` must be the same number.

    Asserted through the helper rather than the raw collector, because the
    helper is what `ResultAssembler._budgets` calls — a test that bypasses it
    would not notice the two label names being swapped.
    """
    set_rate_limit_remaining("tenant_load", "jira", 4871)

    assert sample(RATE_LIMIT_REMAINING_NAME, connector="jira", tenant="tenant_load") == 4871.0


def test_redeclaring_a_histogram_returns_the_existing_collector() -> None:
    """A second import of this module must not raise, and must not shadow.

    `_get_or_create` exists for the gauge already; the histograms use the same
    path, and a `Histogram` registers suffixed sample names as well as its base
    name — so this asserts the lookup key is actually right rather than assuming
    the gauge's behaviour carries over.
    """
    again = _get_or_create(Histogram, QUERY_DURATION_NAME, "ignored")

    assert again is query_duration_seconds


def test_a_non_duplicate_value_error_still_propagates() -> None:
    """LAW 4: only the duplicate case is recovered from, not every ValueError.

    `le` is reserved on a histogram — it is the bucket-boundary label the
    library generates itself. Declaring it is a real mistake with a real
    `ValueError`, and `_get_or_create` must not swallow it just because its
    recovery path happens to catch the same exception type.
    """
    with pytest.raises(ValueError, match="Reserved label"):
        _get_or_create(Histogram, "phase4_probe_seconds", "doc", ("le",))


# --- the route's own timing, including the paths that never return an envelope


class _RaisingRunner:
    async def run(self, body, user, trace_id):
        raise InvalidQueryError("SELECT * is not supported")


class _Request:
    """The two attributes the route actually touches. Infra-free by construction."""

    def __init__(self, runner) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(runner=runner))
        self.state = SimpleNamespace()


async def test_a_failed_query_is_still_counted_in_the_latency_histogram() -> None:
    """ADR-040, and the reason the timer is in the route rather than the runner.

    A malformed query raises before the pipeline produces an envelope. If the
    histogram only saw successes it would report a P95 better than the one
    callers experience — precisely the number a latency metric exists to stop
    anyone believing.
    """
    before = sample(f"{QUERY_DURATION_NAME}_count")

    with pytest.raises(InvalidQueryError):
        await query(body=None, user=None, request=_Request(_RaisingRunner()))

    assert sample(f"{QUERY_DURATION_NAME}_count") == before + 1


async def test_a_missing_runner_is_a_loud_wiring_bug_not_a_silent_empty_result() -> None:
    """Carried from Phase 2: an empty envelope here would be indistinguishable
    from a query that legitimately matched nothing."""
    with pytest.raises(RuntimeError, match="not configured"):
        await query(body=None, user=None, request=_Request(None))
