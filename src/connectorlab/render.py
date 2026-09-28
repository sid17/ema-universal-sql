"""Formatting only. Every value printed here came from the real fetch path."""

import json
from collections.abc import Iterable, Sequence
from typing import Any

from src.connectors.request import OutboundRequest
from src.connectors.response import SourceResponse

WIDTH = 78

#: How much of a response body to show. A page of twenty issues is not more
#: convincing than a page of two, and an artifact nobody scrolls to the end of
#: is an artifact nobody reads.
BODY_LINES = 22


def rule(char: str = "=") -> str:
    return char * WIDTH


def heading(title: str, subtitle: str = "") -> list[str]:
    lines = [rule(), f"  {title}", rule("-")]
    if subtitle:
        lines += [f"  {subtitle}", rule("-")]
    return lines


def note(text: str) -> list[str]:
    """A wrapped aside. Explains *why* a line of output is the shape it is."""
    words, line, out = text.split(), "", []
    for word in words:
        if len(line) + len(word) + 1 > WIDTH - 2:
            out.append(f"  {line}")
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(f"  {line}")
    return out


def table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    """A fixed-width table, sized to its widest cell."""
    body = [[str(cell) for cell in row] for row in rows]
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in body)) if body else len(headers[i])
        for i in range(len(headers))
    ]
    out = ["  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    out.append("  " + "  ".join("-" * w for w in widths))
    for row in body:
        out.append("  " + "  ".join(row[i].ljust(widths[i]) for i in range(len(headers))).rstrip())
    return out


def request_block(outbound: OutboundRequest) -> list[str]:
    """The call, as a wire-shaped request. **Redacted at this boundary only.**

    The credential is still on the real object and still reaches the transport;
    it is removed here because here is where bytes become an artifact.
    """
    safe = outbound.redacted()
    lines = [f"  {safe.method} {safe.target} HTTP/1.1", f"  Host: {safe.host}"]
    lines += [f"  {name}: {value}" for name, value in safe.headers.items()]
    if safe.body:
        lines += ["", *_json_lines(safe.body)]
    return lines


def response_block(response: SourceResponse) -> list[str]:
    """The answer, as a wire-shaped response."""
    lines = [f"  HTTP/1.1 {response.status}"]
    lines += [f"  {name}: {value}" for name, value in response.headers.items()]
    if response.body is not None:
        lines += ["", *_json_lines(response.body)]
    return lines


def rows_block(rows: Sequence[dict[str, Any]], shown: int = 2) -> list[str]:
    """Parsed rows — what the source's body became after field mapping."""
    if not rows:
        return ["  (no rows)"]
    lines = [f"  {json.dumps(row, default=str)}" for row in rows[:shown]]
    if len(rows) > shown:
        lines.append(f"  ... {len(rows) - shown} more")
    return lines


def _json_lines(body: Any) -> list[str]:
    rendered = json.dumps(body, indent=2, default=str).splitlines()
    if len(rendered) <= BODY_LINES:
        return [f"  {line}" for line in rendered]
    kept = [f"  {line}" for line in rendered[:BODY_LINES]]
    kept.append(f"  ... {len(rendered) - BODY_LINES} more lines")
    return kept
