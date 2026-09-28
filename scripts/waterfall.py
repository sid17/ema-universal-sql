#!/usr/bin/env python3
"""Render one trace from ``traces/spans.jsonl`` as a waterfall.

**Standard library only, and that is the point.** ADR-016 declined a tracing
backend and ADR-018 chose JSONL spans, so there is no Jaeger UI to screenshot.
This script is the other half of that bargain: the span log is the artifact, and
this is what makes it readable. Adding matplotlib for one image, or a fourth
container against a sub-60s cold-start gate, would both buy less than they cost.

What it produces is deliberately *text first*. A text waterfall diffs, greps,
pastes into a README and cannot go stale relative to the code that made it. An
SVG is emitted alongside for the PNG the submission gate names by filename.

**It refuses to render an incomplete trace** (LAW 4). ``BatchSpanProcessor``
drops whatever is still queued when the process exits, so the newest trace in
the file is routinely missing its tail — and a waterfall with holes in it reads
as an engine bug rather than as a truncated log. Better to fail naming the
missing spans.

Usage::

    python scripts/waterfall.py                      # newest COMPLETE trace
    python scripts/waterfall.py --trace-id abc123...
    python scripts/waterfall.py --out traces/trace-waterfall.txt --svg traces/trace-waterfall.svg
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_LOG = Path("traces/spans.jsonl")

#: Span attribute carrying a stage's own measured duration. Mirrors
#: ``src.observability.tracing.ELAPSED_MS_ATTRIBUTE`` — duplicated rather than
#: imported so this script stays runnable with no ``src`` on the path.
ELAPSED_MS = "stage.elapsed_ms"

#: Every span a complete ``POST /v1/query`` trace must contain. A trace missing
#: any of these is truncated, not interesting.
REQUIRED = frozenset(
    {"gateway", "parse", "entitlement", "plan", "federation", "assemble", "duckdb_join"}
)

#: At least one source must have been called, or there is no federation to show.
CONNECTOR_PREFIX = "connector."

#: ASGI transport events, hidden unless ``--all`` is passed.
#:
#: These are real spans and this is not a cosmetic filter — but OTel emits one
#: ``http send`` per response chunk, so a single query routinely produces three
#: identical 0.0ms rows. They describe the transport, not the pipeline, and in a
#: waterfall whose whole job is "where did the time go" they read as a
#: rendering fault rather than as information.
TRANSPORT_SUFFIXES = (" http receive", " http send")

#: Sub-cell resolution, so a 3ms stage next to a 400ms one is still visible as
#: something rather than rounding away to nothing.
BLOCKS = " ▏▎▍▌▋▊▉█"


@dataclass(frozen=True)
class Span:
    name: str
    span_id: str
    parent_id: str | None
    start: float
    end: float

    @property
    def duration_ms(self) -> float:
        return (self.end - self.start) * 1000.0


def _ts(raw: str) -> float:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()


def load(path: Path) -> dict[str, list[Span]]:
    """Group every span in the log by trace id.

    Unparseable lines are skipped rather than fatal: the log is append-only and
    a process killed mid-write leaves a partial final line. That is a torn
    record, not a corrupt file, and refusing to read the other 9,000 traces
    because of it would be the wrong trade.
    """
    if not path.exists():
        raise SystemExit(
            f"no span log at {path}. Run a query against the stack first "
            "(OTEL_EXPORTER=file), or pass --file."
        )
    traces: dict[str, list[Span]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
            context = record["context"]
            span = Span(
                name=record["name"],
                span_id=context["span_id"],
                parent_id=record.get("parent_id"),
                start=_ts(record["start_time"]),
                end=_ts(record["end_time"]),
            )
        except (ValueError, KeyError, TypeError):
            continue
        traces.setdefault(context["trace_id"], []).append(span)
    return traces


def missing_from(spans: list[Span]) -> set[str]:
    """What a complete query trace would have that this one does not."""
    names = {span.name for span in spans}
    gaps = set(REQUIRED) - names
    if not any(name.startswith(CONNECTOR_PREFIX) for name in names):
        gaps.add(f"{CONNECTOR_PREFIX}*")
    return gaps


def pick(traces: dict[str, list[Span]], trace_id: str | None) -> tuple[str, list[Span]]:
    """The requested trace, or the newest complete one.

    "Newest" alone is not safe — see the module docstring. Scanning back until a
    complete trace is found is what makes ``make trace`` reproducible instead of
    occasionally rendering whatever the exporter managed to flush.
    """
    if trace_id is not None:
        key = next((k for k in traces if k.endswith(trace_id.removeprefix("0x"))), None)
        if key is None:
            raise SystemExit(f"trace {trace_id} is not in the log ({len(traces)} traces present)")
        gaps = missing_from(traces[key])
        if gaps:
            raise SystemExit(
                f"trace {trace_id} is incomplete — missing {', '.join(sorted(gaps))}. "
                "The batch exporter drops queued spans at exit; allow it to flush "
                "before reading, or pick another trace."
            )
        return key, traces[key]

    for key in sorted(traces, key=lambda k: max(s.end for s in traces[k]), reverse=True):
        if not missing_from(traces[key]):
            return key, traces[key]
    raise SystemExit(
        f"no complete query trace in the log ({len(traces)} traces scanned). "
        f"A complete one contains {', '.join(sorted(REQUIRED))} and at least one "
        f"{CONNECTOR_PREFIX}* span."
    )


def is_transport(span: Span) -> bool:
    return span.name.endswith(TRANSPORT_SUFFIXES)


def ordered(spans: list[Span]) -> list[tuple[int, Span]]:
    """Depth-first, children by start time, as ``(depth, span)``.

    Sorting children by start time is what makes two parallel fetches read as
    parallel: they appear adjacent, and their bars visibly overlap.
    """
    by_parent: dict[str | None, list[Span]] = {}
    known = {span.span_id for span in spans}
    for span in spans:
        parent = span.parent_id if span.parent_id in known else None
        by_parent.setdefault(parent, []).append(span)

    rows: list[tuple[int, Span]] = []

    def walk(parent: str | None, depth: int) -> None:
        for span in sorted(by_parent.get(parent, []), key=lambda s: s.start):
            rows.append((depth, span))
            walk(span.span_id, depth + 1)

    walk(None, 0)
    return rows


def bar(start: float, end: float, width: int) -> str:
    """A proportional bar, positioned on a shared axis by fractional offset."""
    lo, hi = start * width, end * width
    cells = []
    for cell in range(width):
        covered = max(0.0, min(cell + 1, hi) - max(cell, lo))
        cells.append(BLOCKS[min(8, round(covered * 8))])
    rendered = "".join(cells)
    # A span shorter than an eighth of a cell would render as pure whitespace
    # and silently vanish. Give it the thinnest visible mark instead.
    if rendered.strip() == "":
        cells[min(width - 1, int(lo))] = BLOCKS[1]
        rendered = "".join(cells)
    return rendered


def render(trace_id: str, spans: list[Span], width: int) -> str:
    rows = ordered(spans)
    t0 = min(span.start for span in spans)
    total_s = max(span.end for span in spans) - t0
    total_ms = total_s * 1000.0
    labels = [f"{'  ' * depth}{span.name}" for depth, span in rows]
    pad = max(len(label) for label in labels)

    short = trace_id.removeprefix("0x")
    lines = [
        f"trace {short[:8]}…{short[-4:]}   total {total_ms:.1f}ms   spans {len(rows)}",
        "",
    ]
    for label, (_, span) in zip(labels, rows, strict=True):
        start = (span.start - t0) / total_s if total_s else 0.0
        end = (span.end - t0) / total_s if total_s else 1.0
        lines.append(f"{label:<{pad}} │{bar(start, end, width)}│ {span.duration_ms:8.1f}ms")
    return "\n".join(lines) + "\n"


def to_svg(trace_id: str, spans: list[Span], width: int = 900) -> str:
    """The same data as rects, for the ``.png`` the submission gate names."""
    rows = ordered(spans)
    t0 = min(span.start for span in spans)
    total_s = (max(span.end for span in spans) - t0) or 1.0
    label_w, row_h, top = 240, 22, 44
    height = top + row_h * len(rows) + 16
    short = trace_id.removeprefix("0x")
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'font-family="ui-monospace,SFMono-Regular,Menlo,monospace" font-size="12">',
        f'<rect width="{width}" height="{height}" fill="#0d1117"/>',
        f'<text x="16" y="24" fill="#e6edf3" font-size="13">trace {short[:8]}…{short[-4:]}'
        f"   total {total_s * 1000:.1f}ms</text>",
    ]
    span_w = width - label_w - 90
    for index, (depth, span) in enumerate(rows):
        y = top + index * row_h
        x = label_w + span_w * (span.start - t0) / total_s
        w = max(2.0, span_w * (span.end - span.start) / total_s)
        fill = "#f778ba" if span.name.startswith(CONNECTOR_PREFIX) else "#58a6ff"
        parts.append(
            f'<text x="{16 + depth * 12}" y="{y + 12}" fill="#8b949e">{span.name}</text>'
            f'<rect x="{x:.1f}" y="{y + 2}" width="{w:.1f}" height="13" rx="2" fill="{fill}"/>'
            f'<text x="{width - 80}" y="{y + 12}" fill="#8b949e">{span.duration_ms:.1f}ms</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--trace-id", default=None)
    parser.add_argument("--width", type=int, default=48)
    parser.add_argument(
        "--all",
        action="store_true",
        help="include the ASGI http receive/send transport spans",
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--svg", type=Path, default=None)
    args = parser.parse_args(argv)

    trace_id, spans = pick(load(args.file), args.trace_id)
    if not args.all:
        spans = [span for span in spans if not is_transport(span)]
    text = render(trace_id, spans, args.width)
    print(text, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    if args.svg:
        args.svg.parent.mkdir(parents=True, exist_ok=True)
        args.svg.write_text(to_svg(trace_id, spans), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
