"""`/metrics` — against the running stack, asserting SAMPLES rather than names.

The distinction is the whole reason this file exists. `tests/integration/test_scaffold.py`
guarded the gauge with::

    assert "rate_limit_remaining" in body

which is satisfied by the ``# HELP rate_limit_remaining …`` line that Prometheus
emits for a declared-but-never-recorded collector. The gauge was in fact fed by
nothing at all for two phases, and that assertion passed the entire time —
against exactly the broken state it was written to catch (v5 F5).

So every assertion here parses a **labelled sample with a numeric value**. A
metric name in a scrape proves only that somebody declared it.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.usefixtures("reset_state")


def samples(body: str, name: str) -> dict[str, float]:
    """`{label-set: value}` for one metric family, ignoring HELP/TYPE lines."""
    found: dict[str, float] = {}
    for line in body.splitlines():
        if line.startswith("#") or not line.startswith(name):
            continue
        match = re.match(rf"^{re.escape(name)}(\{{.*?\}})?\s+(\S+)$", line)
        if match:
            found[match.group(1) or ""] = float(match.group(2))
    return found


@pytest.fixture
def scrape(client):
    def _scrape() -> str:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        return response.text

    return _scrape


def test_the_rate_limit_gauge_has_a_real_sample_after_a_query(envelope, scrape):
    """**The phase-file acceptance gate, asserted properly.**

    "a /metrics scrape shows the rate_limit_remaining gauge" was previously met
    by a comment line. It is met here by a value.
    """
    envelope()

    found = samples(scrape(), "rate_limit_remaining")

    assert found, "the gauge rendered no samples — it is declared but nothing feeds it"
    labelled = [key for key in found if "github" in key and "tenant_acme" in key]
    assert labelled, f"no github/tenant_acme series; got {sorted(found)}"
    assert found[labelled[0]] >= 0


def test_the_gauge_matches_what_the_envelope_told_the_caller(envelope, scrape):
    """ADR-043: one read feeds both, so they cannot drift. If this ever fails,
    the metric and the API are reporting different budgets for the same bucket —
    which is worse than having no metric, because both look authoritative."""
    env = envelope()
    found = samples(scrape(), "rate_limit_remaining")

    for connector, budget in env["rate_limit_status"].items():
        key = next(k for k in found if f'connector="{connector}"' in k and "tenant_acme" in k)
        assert found[key] == budget["remaining"]


def test_the_connector_histogram_counts_each_source_separately(envelope, scrape):
    """A single aggregate cannot answer "how long did Jira take", which is the
    one question the brief's "1 trace showing connector time" is asking."""
    envelope(max_staleness_ms=0)

    counts = samples(scrape(), "connector_fetch_duration_seconds_count")

    for connector in ("github", "jira"):
        key = next((k for k in counts if f'connector="{connector}"' in k), None)
        assert key is not None, f"no histogram series for {connector}: {sorted(counts)}"
        assert counts[key] > 0


def test_the_slow_source_shows_up_as_slower_in_the_histogram(envelope, scrape):
    """The same claim the waterfall makes, cross-checked from the metric side.

    Jira simulates 180ms and GitHub 40ms (ADR-038), so their histogram sums must
    order the same way. If they do not, either the latency is not being applied
    or the observation is reading the wrong source's timer.
    """
    envelope(max_staleness_ms=0)

    sums = samples(scrape(), "connector_fetch_duration_seconds_sum")
    jira = next(v for k, v in sums.items() if 'connector="jira"' in k)
    github = next(v for k, v in sums.items() if 'connector="github"' in k)

    assert jira > github


def test_query_duration_counts_a_rejected_query_too(client, auth_headers, scrape):
    """ADR-040 — the reason the timer sits in the route and not in the runner.

    A malformed query never reaches the pipeline. A histogram that only saw
    successes would report a P95 better than the one callers actually get, which
    is precisely what a latency metric exists to stop anyone believing.
    """
    before = samples(scrape(), "query_duration_seconds_count").get("", 0.0)

    rejected = client.post(
        "/v1/query",
        headers=auth_headers(),
        json={"sql": "SELECT * FROM github.pull_requests pr", "max_staleness_ms": 0},
    )
    assert rejected.status_code == 400, rejected.text

    after = samples(scrape(), "query_duration_seconds_count").get("", 0.0)
    assert after == before + 1


def test_the_golden_signals_and_our_own_metrics_share_one_scrape(envelope, scrape):
    """ADR-015's single assumption. If a future instrumentator release splits
    the registry, `/metrics` silently halves — this fails instead."""
    envelope()
    body = scrape()

    assert samples(body, "http_requests_total"), "instrumentator collectors missing"
    assert samples(body, "query_duration_seconds_count"), "our histogram missing"
    assert samples(body, "rate_limit_remaining"), "our gauge missing"
