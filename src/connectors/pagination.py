"""Pagination as *strategy* ⊕ *placement*, decoupled (research Card 2).

The **strategy** computes the next token; the **placement** — a
:class:`~src.connectors.base.RequestOption` on the capability model — says where
that token is injected into the outgoing call. Keeping them separate is what
lets one contract serve GitHub's cursor and Jira's ``startAt`` without either
adapter special-casing the other.

**The token this module hands out is opaque and self-describing.** Two reasons,
both about what a caller could otherwise do by accident:

1. A raw row index invites Phase 2 (or a UI) to construct a cursor by hand —
   ``page = str(offset + limit)`` — which couples the caller to the mock's
   internal ordering. When a live adapter later replaces the mock, every
   hand-built cursor breaks in a way that looks like a data bug.
2. The token names the strategy that issued it, so a GitHub cursor fed to the
   Jira adapter is rejected rather than silently reinterpreted as an offset into
   a different dataset — which would return real rows for the wrong question.

What is deliberately *not* built: literal ``Link:`` header formatting. A header
is a transport detail of a real HTTP call, and no mock makes one. The pattern
worth proving is the decoupling, not the string (DoD §3 tiers header simulation
COULD).
"""

import base64
import binascii
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from src.connectors.base import PaginationSpec

#: Bumped if the token payload shape ever changes, so an old cursor is rejected
#: with a clear message instead of being misread under the new shape.
TOKEN_VERSION = 1


@dataclass(frozen=True)
class Page:
    """One page of rows, plus how to ask for the next."""

    rows: list[dict[str, Any]]
    next_cursor: str | None
    has_more: bool


def encode_token(strategy: str, offset: int) -> str:
    """Opaque, URL-safe, and carrying the strategy that issued it."""
    payload = json.dumps({"v": TOKEN_VERSION, "s": strategy, "o": offset}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_token(token: str, expected_strategy: str) -> int:
    """Return the offset inside ``token``, or raise.

    LAW 4: every failure raises rather than falling back to offset 0. Silently
    restarting at the first page would hand a caller paging through results a
    duplicate page and no indication anything went wrong — the caller would
    conclude the data was wrong, not the cursor.
    """
    padding = "=" * (-len(token) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(token + padding))
    except (binascii.Error, ValueError, TypeError) as exc:
        raise ValueError(f"malformed page cursor {token!r}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"malformed page cursor {token!r}")
    if payload.get("v") != TOKEN_VERSION:
        raise ValueError(
            f"page cursor {token!r} was issued for token version {payload.get('v')!r}, "
            f"this build expects {TOKEN_VERSION}"
        )
    if payload.get("s") != expected_strategy:
        raise ValueError(
            f"page cursor {token!r} was issued by the {payload.get('s')!r} strategy, "
            f"but was presented to the {expected_strategy!r} strategy"
        )
    offset = payload.get("o")
    if not isinstance(offset, int) or offset < 0:
        raise ValueError(f"page cursor {token!r} carries an invalid offset {offset!r}")
    return offset


class PaginationStrategy(ABC):
    """Slices an already-filtered row list into pages."""

    name: str

    def __init__(self, page_size: int) -> None:
        if page_size <= 0:
            raise ValueError(f"page_size must be positive (got {page_size})")
        self.page_size = page_size

    def paginate(
        self, rows: list[dict[str, Any]], limit: int | None = None, page: str | None = None
    ) -> Page:
        """Return the page starting at ``page``, at most ``limit`` rows long.

        ``limit`` is clamped to the source's ``page_size``: a caller asking for
        more than the source will return in one call must still be told there is
        more to come, rather than being handed a short page that looks terminal.
        """
        effective = self.page_size if limit is None else min(limit, self.page_size)
        if effective <= 0:
            raise ValueError(f"limit must be positive (got {limit})")

        offset = 0 if page is None else decode_token(page, self.name)
        window = rows[offset : offset + effective]
        consumed = offset + len(window)
        has_more = consumed < len(rows)
        return Page(
            rows=window,
            next_cursor=self.next_token(consumed) if has_more else None,
            has_more=has_more,
        )

    @abstractmethod
    def next_token(self, consumed: int) -> str:
        """The token that asks for the rows after ``consumed``."""

    @abstractmethod
    def wire_value(self, page: str | None) -> str | None:
        """What this page token looks like **on the wire**, or ``None`` to omit it.

        The other half of "strategy ⊕ placement": ``token_option`` says *where*
        the token goes, this says *what* is actually sent. The two strategies
        disagree on both counts — GitHub omits the cursor entirely on the first
        page, where a real Jira client always sends ``startAt=0``.
        """


class CursorStrategy(PaginationStrategy):
    """GitHub-style: an opaque cursor pointing past the last row returned."""

    name = "cursor"

    def next_token(self, consumed: int) -> str:
        return encode_token(self.name, consumed)

    def wire_value(self, page: str | None) -> str | None:
        """The opaque cursor, verbatim — and nothing at all on the first page."""
        if page is None:
            return None
        decode_token(page, self.name)  # reject a foreign token before sending it
        return page


class OffsetStrategy(PaginationStrategy):
    """Jira-style: ``startAt`` / ``total``.

    The wire protocol it models is a plain integer offset, but the token handed
    to *our* callers is still encoded — see the module docstring. The strategy
    is what differs from :class:`CursorStrategy`; the opacity is a property of
    the contract, not of the source.
    """

    name = "offset"

    def next_token(self, consumed: int) -> str:
        return encode_token(self.name, consumed)

    @staticmethod
    def start_at(page: str | None) -> int:
        """The ``startAt`` value this page corresponds to on the wire.

        Exposed so an adapter can show what it *would* send to a real Jira,
        which is the half of "strategy ⊕ placement" that the opaque token hides.
        """
        return 0 if page is None else decode_token(page, OffsetStrategy.name)

    def wire_value(self, page: str | None) -> str | None:
        """Always sent, ``startAt=0`` included — which is what a real Jira client does."""
        return str(self.start_at(page))


_STRATEGIES: dict[str, type[PaginationStrategy]] = {
    CursorStrategy.name: CursorStrategy,
    OffsetStrategy.name: OffsetStrategy,
}


def strategy_for(spec: PaginationSpec) -> PaginationStrategy:
    """Build the strategy a seeded capability model asks for.

    Raises on an unknown strategy rather than defaulting: a typo in a YAML
    capability file must fail at seed time, not silently page a source the
    wrong way.
    """
    try:
        return _STRATEGIES[spec.strategy](spec.page_size)
    except KeyError:
        raise ValueError(
            f"unknown pagination strategy {spec.strategy!r}; expected one of {sorted(_STRATEGIES)}"
        ) from None
