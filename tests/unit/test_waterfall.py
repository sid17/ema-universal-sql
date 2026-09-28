"""The waterfall renderer (ADR-041).

Infra-free: every test builds its own span log in a tmp file, so none of this
depends on a running stack or on whatever `traces/spans.jsonl` happens to hold.

The assertions that matter are not "it draws bars". They are:

  * an **incomplete** trace is refused, loudly, naming what is missing — because
    `BatchSpanProcessor` drops its queue at exit, so a truncated trace is the
    normal case, not an exotic one, and a waterfall with holes in it reads as an
    engine bug rather than as a truncated log (LAW 4);
  * bar **position** encodes start time, not just duration — which is what makes
    two parallel fetches look parallel;
  * a sub-millisecond span still renders as *something*.
"""

from __future__ import annotations

import json

import pytest

from scripts.waterfall import (
    ELAPSED_MS,
    bar,
    is_transport,
    load,
    main,
    missing_from,
    ordered,
    pick,
    render,
    to_svg,
)

TRACE = "0xaaaabbbbccccddddeeeeffff00001111"


def stamp(ms: int) -> str:
    """`ms` after 10:00:00, in the ISO-8601 shape the exporter writes."""
    return f"2026-09-28T10:00:{ms // 1000:02d}.{ms % 1000:03d}000Z"


def span(name, span_id, parent, start_ms, end_ms, trace=TRACE) -> str:
    """One JSONL record in the exact shape ConsoleSpanExporter writes."""
    return json.dumps(
        {
            "name": name,
            "context": {"trace_id": trace, "span_id": span_id, "trace_state": "[]"},
            "kind": "SpanKind.INTERNAL",
            "parent_id": parent,
            "start_time": stamp(start_ms),
            "end_time": stamp(end_ms),
            "status": {"status_code": "UNSET"},
            "attributes": {ELAPSED_MS: float(end_ms - start_ms)},
        }
    )


#: A complete query trace: the seven required spans plus two sources, with the
#: two connector fetches deliberately OVERLAPPING.
COMPLETE = [
    span("gateway", "0x01", None, 0, 300),
    span("parse", "0x02", "0x01", 2, 6),
    span("entitlement", "0x03", "0x01", 6, 7),
    span("plan", "0x04", "0x01", 7, 8),
    span("federation", "0x05", "0x01", 8, 250),
    span("connector.github", "0x06", "0x05", 10, 60),
    span("connector.jira", "0x07", "0x05", 10, 200),
    span("duckdb_join", "0x08", "0x05", 200, 250),
    span("assemble", "0x09", "0x01", 250, 252),
]


@pytest.fixture
def log(tmp_path):
    def _write(lines) -> object:
        path = tmp_path / "spans.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    return _write


# --- completeness: the check that keeps a truncated log from looking like a bug


def test_a_complete_trace_is_accepted(log):
    trace_id, spans = pick(load(log(COMPLETE)), None)

    assert trace_id == TRACE
    assert len(spans) == len(COMPLETE)


def test_a_truncated_trace_is_refused_and_names_what_is_missing(log):
    """The batch exporter drops queued spans at exit, so this is the COMMON
    case. Rendering it anyway would produce an artifact with holes that a
    reviewer would read as an engine fault."""
    without_join = [line for line in COMPLETE if '"duckdb_join"' not in line]

    with pytest.raises(SystemExit) as exit_info:
        pick(load(log(without_join)), None)

    assert "duckdb_join" in str(exit_info.value)


def test_a_trace_with_no_connector_span_is_refused(log):
    """No source was called, so there is no federation to show — and the
    waterfall's entire subject is where the source time went."""
    no_sources = [line for line in COMPLETE if "connector." not in line]

    assert "connector.*" in missing_from([])
    with pytest.raises(SystemExit) as exit_info:
        pick(load(log(no_sources)), None)
    assert "connector.*" in str(exit_info.value)


def test_an_explicitly_requested_incomplete_trace_still_refuses(log):
    """`--trace-id` is a convenience, not an override. Silently rendering a
    partial tree because the caller named it would defeat the whole check."""
    without_join = [line for line in COMPLETE if '"duckdb_join"' not in line]

    with pytest.raises(SystemExit, match="incomplete"):
        pick(load(log(without_join)), TRACE)


def test_an_unknown_trace_id_says_so_rather_than_rendering_something_else(log):
    with pytest.raises(SystemExit, match="not in the log"):
        pick(load(log(COMPLETE)), "0xdeadbeef")


def test_the_newest_complete_trace_wins_over_a_newer_truncated_one(log):
    """The exact situation `make trace` creates: the most recent trace is the
    one most likely to be mid-flush."""
    older = "0x1111" + "0" * 28
    complete_but_older = [line.replace(TRACE, older) for line in COMPLETE]
    newer_truncated = [line for line in COMPLETE if '"assemble"' not in line]

    trace_id, _ = pick(load(log(complete_but_older + newer_truncated)), None)

    assert trace_id == older


def test_a_torn_final_line_does_not_lose_the_rest_of_the_log(log):
    """An append-only log killed mid-write leaves a partial record. That is one
    torn line, not a corrupt file — refusing the other spans would be wrong."""
    trace_id, spans = pick(load(log([*COMPLETE, '{"name": "gateway", "conte'])), None)

    assert len(spans) == len(COMPLETE)
    assert trace_id == TRACE


# --- layout: position carries information, not only length ------------------


def bars(text: str) -> dict[str, str]:
    """`{span name: the drawn bar}` for every row of a rendered waterfall."""
    rows = {}
    for line in text.splitlines():
        if "│" in line:
            label, drawn, *_ = line.split("│")
            rows[label.strip()] = drawn
    return rows


def extent(drawn: str) -> tuple[int, int]:
    """`(first, last)` column carrying ink."""
    inked = [i for i, char in enumerate(drawn) if char != " "]
    return inked[0], inked[-1]


def test_a_bar_starts_where_its_span_starts(log):
    """Position carries information, not only length.

    `duckdb_join` begins at 200ms and `connector.github` at 10ms, so the join's
    first ink must sit strictly to the right. **This is the assertion that fails
    if bars are laid out by duration from a common left edge** — a layout which
    still draws every bar at a plausible width and is therefore invisible to any
    test that only measures how much ink a row has.
    """
    _, spans = pick(load(log(COMPLETE)), None)
    rows = bars(render(TRACE, spans, width=48))

    assert extent(rows["duckdb_join"])[0] > extent(rows["connector.github"])[0]
    assert extent(rows["connector.github"])[0] > extent(rows["parse"])[0]


def test_parallel_spans_overlap_and_sequential_ones_do_not(log):
    """**The assertion the artifact's credibility rests on.**

    The two fetches run concurrently (10-60ms and 10-200ms) and the join runs
    after both (200-250ms). Asserting the overlap alone would pass against a
    renderer that starts every bar at the left edge, where *everything*
    overlaps — so the disjoint pair is asserted in the same test, and it is the
    half that does the work.
    """
    _, spans = pick(load(log(COMPLETE)), None)
    rows = bars(render(TRACE, spans, width=48))

    github_lo, github_hi = extent(rows["connector.github"])
    jira_lo, jira_hi = extent(rows["connector.jira"])
    join_lo, _ = extent(rows["duckdb_join"])

    assert github_lo <= jira_hi and jira_lo <= github_hi, "parallel fetches must overlap"
    assert join_lo > github_hi, "a span that starts after another ends must not overlap it"


def test_a_longer_span_gets_a_longer_bar(log):
    _, spans = pick(load(log(COMPLETE)), None)
    text = render(TRACE, spans, width=48)

    ink = {name: sum(c != " " for c in drawn) for name, drawn in bars(text).items()}

    assert ink["connector.jira"] > ink["connector.github"]
    assert ink["federation"] > ink["connector.jira"]


def test_a_sub_millisecond_span_still_renders_something():
    """A stage that rounds to zero width would vanish, and a missing row reads
    as a stage that never ran."""
    drawn = bar(0.5, 0.5001, width=48)

    assert drawn.strip() != ""


def test_children_are_indented_under_their_parent(log):
    _, spans = pick(load(log(COMPLETE)), None)
    depths = {span.name: depth for depth, span in ordered(spans)}

    assert depths["gateway"] == 0
    assert depths["federation"] == 1
    assert depths["connector.jira"] == 2


def test_a_span_whose_parent_is_absent_is_treated_as_a_root(log):
    """Otherwise a trace whose root was dropped by the exporter would render
    nothing at all rather than rendering what survived."""
    orphans = [line for line in COMPLETE if '"gateway"' not in line]
    spans = load(log(orphans))[TRACE]

    assert {depth for depth, _ in ordered(spans)} != set()


# --- the failure the renderer must survive ----------------------------------


def test_a_timed_out_source_still_renders_and_dominates(log):
    """The worst case must be the MOST legible one — a trace captured during a
    timeout is exactly the one someone reaches for during an incident.

    The timed-out source runs past its parent's end, which is the shape a
    `wait_for` cancellation actually produces; the renderer must not clip it or
    divide by a total that excludes it.
    """
    timed_out = [line for line in COMPLETE if '"connector.jira"' not in line]
    timed_out.append(span("connector.jira", "0x07", "0x05", 10, 4000))

    trace_id, spans = pick(load(log(timed_out)), None)
    rows = bars(render(trace_id, spans, width=48))

    jira_ink = sum(c != " " for c in rows["connector.jira"])
    github_ink = sum(c != " " for c in rows["connector.github"])
    assert jira_ink > github_ink


def test_transport_spans_are_recognised():
    """Hidden by default: OTel emits one `http send` per response chunk, so a
    single query routinely produces three identical 0.0ms rows that describe the
    transport rather than the pipeline."""
    from scripts.waterfall import Span

    asgi = Span("POST /v1/query http send", "0x1", "0x2", 0.0, 0.0)
    stage = Span("federation", "0x1", "0x2", 0.0, 0.1)

    assert is_transport(asgi)
    assert not is_transport(stage)


def test_the_svg_carries_every_span(log):
    _, spans = pick(load(log(COMPLETE)), None)

    svg = to_svg(TRACE, spans)

    assert svg.startswith("<svg")
    for name in ("connector.github", "connector.jira", "duckdb_join"):
        assert name in svg


def test_a_missing_log_file_explains_itself(tmp_path):
    """LAW 4: a reviewer who runs this before starting the stack gets a
    sentence, not a traceback."""
    with pytest.raises(SystemExit, match="no span log"):
        load(tmp_path / "nope.jsonl")


def test_main_writes_both_artifacts(log, tmp_path):
    out, svg = tmp_path / "w.txt", tmp_path / "w.svg"

    assert main(["--file", str(log(COMPLETE)), "--out", str(out), "--svg", str(svg)]) == 0
    assert "connector.jira" in out.read_text()
    assert svg.read_text().startswith("<svg")
