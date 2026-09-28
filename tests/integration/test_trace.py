"""The trace a query leaves behind — the submission artifact's own gate.

DoD §1 gate 5 wants "one metrics/trace screenshot and a note on what it proves",
and the phase file's acceptance line asks that "a query's `trace_id` resolves to
a span set". This is that assertion, run against the real stack: the id the
caller was handed in the envelope must actually find the spans, or `trace_id` is
a decorative UUID rather than a correlation key.

**Why this needs to poll.** `BatchSpanProcessor` exports on a timer (ADR-018
chose it over `SimpleSpanProcessor` precisely so export cost stays off the
request path), so the spans are not in the file the instant the response
returns. Polled rather than slept, so the test ends the moment the data lands
and fails with a real message if it never does.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

#: Bind-mounted by docker-compose, so the host sees what the container writes.
SPAN_LOG = Path("traces/spans.jsonl")

FLUSH_TIMEOUT_S = 30.0

#: The seven the phase file specifies. `federation` is the parent the two
#: connector spans hang from; without it the waterfall has no tree.
EXPECTED = {"gateway", "parse", "entitlement", "plan", "federation", "assemble", "duckdb_join"}


def spans_for(trace_id: str) -> list[dict]:
    """Every span recorded under `trace_id`, as written to the JSONL sink."""
    if not SPAN_LOG.exists():
        return []
    found = []
    for line in SPAN_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a torn final line from an append-mode writer
        if record.get("context", {}).get("trace_id", "").removeprefix("0x") == trace_id:
            found.append(record)
    return found


def await_trace(trace_id: str, required: set[str]) -> list[dict]:
    deadline = time.monotonic() + FLUSH_TIMEOUT_S
    seen: set[str] = set()
    while time.monotonic() < deadline:
        found = spans_for(trace_id)
        seen = {record["name"] for record in found}
        if required <= seen:
            return found
        time.sleep(0.5)
    pytest.fail(
        f"trace {trace_id} never carried {sorted(required - seen)} within "
        f"{FLUSH_TIMEOUT_S}s. Present: {sorted(seen) or 'nothing — is OTEL_EXPORTER=file?'}",
        pytrace=False,
    )


@pytest.fixture(autouse=True)
def require_span_sink():
    if not SPAN_LOG.exists():
        pytest.skip(
            f"{SPAN_LOG} does not exist. These tests read the bind-mounted span "
            "log; run `make up` from the repo root with OTEL_EXPORTER=file."
        )


def test_the_envelopes_trace_id_resolves_to_a_real_span_set(envelope):
    """The claim `trace_id` makes. Without this it is a decorative UUID."""
    env = envelope(max_staleness_ms=0)

    found = await_trace(env["trace_id"], EXPECTED)

    assert {record["name"] for record in found} >= EXPECTED


def test_the_connector_spans_are_parented_to_federation(envelope):
    """**The evidence the federation is parallel.**

    Two sibling spans under one parent, with overlapping wall-clock intervals,
    is what a waterfall renders as concurrency. Spans synthesized afterwards
    from `stats.connector_ms` would carry invented start times and could only
    ever render as sequential (ADR-037).
    """
    env = envelope(max_staleness_ms=0)
    found = await_trace(env["trace_id"], EXPECTED | {"connector.github", "connector.jira"})

    by_id = {record["context"]["span_id"]: record for record in found}
    connectors = [r for r in found if r["name"].startswith("connector.")]

    assert len(connectors) == 2
    for record in connectors:
        assert by_id[record["parent_id"]]["name"] == "federation"


def test_the_span_durations_agree_with_the_envelope(envelope):
    """Three views of one measurement — span, histogram, envelope (ADR-037).

    If these disagree the trace is not describing the request the caller got,
    and every conclusion drawn from the waterfall is unsound.
    """
    env = envelope(max_staleness_ms=0)
    found = await_trace(env["trace_id"], EXPECTED | {"connector.jira"})

    jira = next(r for r in found if r["name"] == "connector.jira")
    reported = env["stats"]["connector_ms"]["jira"]

    assert jira["attributes"]["stage.elapsed_ms"] == pytest.approx(reported, rel=0.01)


def test_the_slow_source_dominates_the_trace(envelope):
    """**"P95 was Jira, not the engine" — the phase's Done-when sentence.**

    Asserted rather than captioned: Jira's span must be wider than every engine
    stage, or the artifact is telling a story the data does not support.
    """
    env = envelope(max_staleness_ms=0)
    found = await_trace(env["trace_id"], EXPECTED | {"connector.jira"})

    elapsed = {r["name"]: r["attributes"].get("stage.elapsed_ms", 0.0) for r in found}

    for engine_stage in ("parse", "entitlement", "plan", "assemble"):
        assert elapsed["connector.jira"] > elapsed[engine_stage]


def test_a_cache_hit_leaves_a_trace_too(envelope):
    """A fast request must still be traceable — otherwise the only traces that
    exist are the slow ones, and the trace set is silently biased."""
    envelope(max_staleness_ms=0)
    env = envelope(max_staleness_ms=60_000)

    found = await_trace(env["trace_id"], EXPECTED)

    assert {record["name"] for record in found} >= EXPECTED
