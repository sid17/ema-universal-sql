"""The spans and the histogram `FederationEngine` emits (ADR-037).

Split from `test_federation.py` when that file crossed LAW 1's hard 500-line
limit. The seam is by subject: that file asserts what the engine *returns* —
rows, partial results, the deadline, the trichotomy; this one asserts what it
*records* about itself.

Three of these are the reason the phase exists. Before it, the five pipeline
stage spans stopped at one opaque `federation` span containing both connector
fetches and the DuckDB join, so the waterfall could not answer the only question
it is for: which source cost the time. The other two pin the failure paths,
because a span that leaks on a timeout would leave exactly the trace someone
reaches for during an incident missing its slowest bar.
"""

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY

from src.connectors.errors import FailureMode
from src.execution.federation import SOURCE_STATE_ATTRIBUTE
from src.execution.join import JOIN_SPAN
from src.models.errors import ApiError
from src.observability.metrics import CONNECTOR_FETCH_DURATION_NAME
from src.observability.tracing import ELAPSED_MS_ATTRIBUTE, reset_tracing
from tests.unit.test_federation import SlowAdapter, make_plan, run


@pytest.fixture
def spans():
    """Bind the tracer provider to an in-memory exporter for one test."""
    exporter = InMemorySpanExporter()
    reset_tracing(exporter)
    yield exporter
    reset_tracing()


def names(exporter) -> set[str]:
    return {span.name for span in exporter.get_finished_spans()}


def span_named(exporter, name):
    return next(span for span in exporter.get_finished_spans() if span.name == name)


async def test_every_source_and_the_join_get_their_own_span(parser, adapters, spans):
    """Five stage spans existed before this phase; these three did not.

    Without them the waterfall bottoms out at one opaque `federation` bar, and
    the phase's headline reading — "P95 was Jira, not the engine" — cannot be
    taken off the trace at all.
    """
    await run(adapters, make_plan(parser))

    assert {"connector.github", "connector.jira", JOIN_SPAN} <= names(spans)


async def test_the_two_connector_spans_overlap_in_time(parser, adapters, spans):
    """**The assertion that proves the federation is actually parallel.**

    A span set synthesized afterwards from `stats.connector_ms` would carry
    invented start times and render the two fetches as sequential. These are
    opened inside the `asyncio.gather` closure, so their real wall-clock
    intervals intersect — and that intersection is the evidence.
    """
    adapters["jira"] = SlowAdapter(adapters["jira"], delay_s=0.05)
    await run(adapters, make_plan(parser))

    github = span_named(spans, "connector.github")
    jira = span_named(spans, "connector.jira")
    assert github.start_time < jira.end_time
    assert jira.start_time < github.end_time


async def test_the_span_records_the_same_elapsed_ms_the_envelope_reports(
    parser, adapters, spans
):
    """One `perf_counter` pair, three views (span, histogram, envelope).

    A second timer inside the span would produce a number that could disagree
    with the response — the one thing a trace must never do.
    """
    result = await run(adapters, make_plan(parser))

    jira_fetch = next(f for f in result.fetches if f.connector_type == "jira")
    assert span_named(spans, "connector.jira").attributes[ELAPSED_MS_ATTRIBUTE] == pytest.approx(
        jira_fetch.elapsed_ms
    )


async def test_the_span_still_closes_when_the_source_times_out(parser, adapters, spans):
    """The worst case must be the *most* legible one, not the least.

    A span that leaks on the timeout path would leave exactly the trace a
    reviewer reaches for during an incident missing its slowest bar.
    """
    adapters["jira"] = SlowAdapter(adapters["jira"], delay_s=0.5)
    await run(adapters, make_plan(parser), deadline_ms=100)

    jira = span_named(spans, "connector.jira")
    assert jira.attributes[SOURCE_STATE_ATTRIBUTE] == "timeout"
    assert jira.end_time is not None


async def test_the_span_still_closes_when_the_source_raises(parser, adapters, spans):
    """Same guarantee on the hard-failure path, which raises out of `execute`."""
    adapters["jira"].fail_next(FailureMode.AUTH)
    with pytest.raises(ApiError):
        await run(adapters, make_plan(parser))

    assert span_named(spans, "connector.jira").attributes[SOURCE_STATE_ATTRIBUTE] == "error"


async def test_a_source_that_is_never_called_gets_no_span(parser, adapters, spans):
    """Default-deny skips the adapter entirely (non-negotiable #1).

    A span for a call that never happened would put a bar on the waterfall for
    work nobody did, and would make the trace disagree with the access log.
    """
    plan = make_plan(parser, roles=("contractor",))
    await run(adapters, plan)

    assert "connector.jira" not in names(spans)
    assert "connector.github" in names(spans)


async def test_the_connector_histogram_records_every_outcome(parser, adapters, spans):
    """Timeouts count too — a histogram that only sees successes reports a P95
    better than the one users actually get."""
    before = REGISTRY.get_sample_value(
        f"{CONNECTOR_FETCH_DURATION_NAME}_count", {"connector": "jira"}
    ) or 0.0
    adapters["jira"].fail_next(FailureMode.TIMEOUT)

    await run(adapters, make_plan(parser))

    after = REGISTRY.get_sample_value(
        f"{CONNECTOR_FETCH_DURATION_NAME}_count", {"connector": "jira"}
    )
    assert after == before + 1


async def test_a_skipped_source_is_not_counted_in_the_histogram(parser, adapters, spans):
    """A 0ms sample for a fetch that did not happen would drag the histogram
    down and make a default-denied query look like a fast one."""
    before = REGISTRY.get_sample_value(
        f"{CONNECTOR_FETCH_DURATION_NAME}_count", {"connector": "jira"}
    ) or 0.0

    await run(adapters, make_plan(parser, roles=("contractor",)))

    after = REGISTRY.get_sample_value(
        f"{CONNECTOR_FETCH_DURATION_NAME}_count", {"connector": "jira"}
    ) or 0.0
    assert after == before
