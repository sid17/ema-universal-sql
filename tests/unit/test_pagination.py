"""Pagination — gate test ``test_pagination``.

Both strategies are exercised: GitHub's cursor and Jira's offset. The decisive
assertion is that page 2 continues with **zero overlap** and that the pages
reassemble into exactly the full set — a test that only counted rows would pass
against a paginator that returned the same page twice.
"""

import pytest

from src.connectors.base import PaginationSpec, RequestOption
from src.connectors.pagination import (
    CursorStrategy,
    OffsetStrategy,
    decode_token,
    encode_token,
    strategy_for,
)

ROWS = [{"id": n, "name": f"row-{n:02d}"} for n in range(20)]

CURSOR_SPEC = PaginationSpec(
    strategy="cursor",
    page_size=100,
    token_option=RequestOption("query", "cursor"),
    size_option=RequestOption("query", "per_page"),
    stop="returned<page_size",
)
OFFSET_SPEC = PaginationSpec(
    strategy="offset",
    page_size=100,
    token_option=RequestOption("query", "startAt"),
    size_option=RequestOption("query", "maxResults"),
    stop="returned<page_size",
)


@pytest.fixture(params=["cursor", "offset"])
def strategy(request):
    """Every structural property must hold for BOTH strategies."""
    return {"cursor": CursorStrategy, "offset": OffsetStrategy}[request.param](page_size=100)


# --- GATE: test_pagination -------------------------------------------------


def test_pagination(strategy):
    """limit=5 -> 5 rows, has_more, a cursor; the next page continues cleanly."""
    first = strategy.paginate(ROWS, limit=5, page=None)
    assert len(first.rows) == 5
    assert first.has_more is True
    assert first.next_cursor is not None

    second = strategy.paginate(ROWS, limit=5, page=first.next_cursor)
    assert len(second.rows) == 5
    assert second.has_more is True

    # Zero overlap, and in order.
    first_ids = [r["id"] for r in first.rows]
    second_ids = [r["id"] for r in second.rows]
    assert first_ids == [0, 1, 2, 3, 4]
    assert second_ids == [5, 6, 7, 8, 9]
    assert set(first_ids).isdisjoint(second_ids)


def test_full_walk_reassembles_the_exact_set(strategy):
    """Every row exactly once — no gaps, no repeats.

    The property a row-count assertion cannot check: a paginator returning page
    1 twice would still yield the right total.
    """
    seen, page, guard = [], None, 0
    while True:
        guard += 1
        assert guard < 100, "pagination did not terminate"
        result = strategy.paginate(ROWS, limit=5, page=page)
        seen.extend(result.rows)
        if not result.has_more:
            assert result.next_cursor is None
            break
        page = result.next_cursor
    assert seen == ROWS


def test_last_page_is_terminal(strategy):
    """has_more False and next_cursor None must arrive together."""
    result = strategy.paginate(ROWS, limit=20, page=None)
    assert len(result.rows) == 20
    assert result.has_more is False
    assert result.next_cursor is None


def test_a_partial_final_page_is_terminal(strategy):
    """The universal stop condition: returned < page_size."""
    result = strategy.paginate(ROWS[:3], limit=5, page=None)
    assert len(result.rows) == 3
    assert result.has_more is False
    assert result.next_cursor is None


def test_empty_result_is_terminal_not_an_error(strategy):
    """`empty` is a legitimate answer, distinct from `error` (DoD §4)."""
    result = strategy.paginate([], limit=5, page=None)
    assert result.rows == []
    assert result.has_more is False
    assert result.next_cursor is None


def test_exact_multiple_does_not_advertise_a_phantom_page(strategy):
    """20 rows at 5 per page: the 4th page must be the last.

    An off-by-one here would hand back a cursor to an empty 5th page.
    """
    page, pages = None, 0
    while True:
        result = strategy.paginate(ROWS, limit=5, page=page)
        pages += 1
        if not result.has_more:
            break
        page = result.next_cursor
    assert pages == 4


def test_limit_is_clamped_to_the_source_page_size():
    """Asking for more than the source returns must still report has_more.

    Otherwise the caller gets a short page with no cursor and concludes it has
    seen everything.
    """
    small = CursorStrategy(page_size=5)
    result = small.paginate(ROWS, limit=1000, page=None)
    assert len(result.rows) == 5
    assert result.has_more is True
    assert result.next_cursor is not None


@pytest.mark.parametrize("limit", [0, -1])
def test_nonsense_limits_are_rejected(strategy, limit):
    with pytest.raises(ValueError, match="limit must be positive"):
        strategy.paginate(ROWS, limit=limit)


def test_zero_page_size_is_rejected_at_construction():
    with pytest.raises(ValueError, match="page_size must be positive"):
        CursorStrategy(page_size=0)


# --- the token is opaque and self-describing -------------------------------


def test_cursor_is_not_a_readable_offset(strategy):
    """A caller must not be able to guess `page = str(offset + limit)`."""
    cursor = strategy.paginate(ROWS, limit=5).next_cursor
    assert cursor != "5"
    assert not cursor.isdigit()


def test_token_round_trips():
    assert decode_token(encode_token("cursor", 17), "cursor") == 17


def test_a_cursor_from_one_strategy_is_rejected_by_the_other():
    """The property that makes cross-feeding a cursor loud instead of silent.

    Reinterpreted as an offset into a different dataset, a foreign cursor would
    return real rows for the wrong question.
    """
    github_cursor = CursorStrategy(page_size=100).paginate(ROWS, limit=5).next_cursor
    with pytest.raises(ValueError, match="was issued by the 'cursor' strategy"):
        OffsetStrategy(page_size=100).paginate(ROWS, limit=5, page=github_cursor)


@pytest.mark.parametrize("bad", ["", "!!!!", "zzzz", "not-a-cursor"])
def test_malformed_cursor_raises_rather_than_restarting(strategy, bad):
    """LAW 4.

    Falling back to offset 0 would hand a paging caller a duplicate page with no
    signal — they would conclude the data was wrong, not the cursor.
    """
    with pytest.raises(ValueError):
        strategy.paginate(ROWS, limit=5, page=bad)


def test_cursor_from_a_future_token_version_is_rejected():
    from src.connectors import pagination

    forged = pagination.encode_token("cursor", 5)
    original = pagination.TOKEN_VERSION
    try:
        pagination.TOKEN_VERSION = original + 1
        with pytest.raises(ValueError, match="token version"):
            pagination.decode_token(forged, "cursor")
    finally:
        pagination.TOKEN_VERSION = original


def test_negative_offset_in_a_forged_token_is_rejected():
    forged = encode_token("cursor", -5)
    with pytest.raises(ValueError, match="invalid offset"):
        decode_token(forged, "cursor")


# --- strategy ⊕ placement --------------------------------------------------


def test_strategy_is_built_from_the_seeded_spec():
    assert isinstance(strategy_for(CURSOR_SPEC), CursorStrategy)
    assert isinstance(strategy_for(OFFSET_SPEC), OffsetStrategy)
    assert strategy_for(CURSOR_SPEC).page_size == 100


def test_unknown_strategy_in_yaml_fails_loudly():
    """A typo in a capability file must fail, not silently page the wrong way."""
    typo = PaginationSpec(
        strategy="curser",
        page_size=100,
        token_option=RequestOption("query", "cursor"),
        size_option=RequestOption("query", "per_page"),
        stop="returned<page_size",
    )
    with pytest.raises(ValueError, match="unknown pagination strategy"):
        strategy_for(typo)


def test_placement_is_independent_of_strategy():
    """The Card 2 decoupling: same strategy, different injection field."""
    assert CURSOR_SPEC.token_option.field == "cursor"
    assert OFFSET_SPEC.token_option.field == "startAt"
    assert CURSOR_SPEC.token_option.inject_into == OFFSET_SPEC.token_option.inject_into


def test_offset_strategy_exposes_its_wire_start_at():
    """What a real Jira call would carry, behind the opaque token."""
    jira = OffsetStrategy(page_size=100)
    assert jira.start_at(None) == 0
    cursor = jira.paginate(ROWS, limit=5).next_cursor
    assert jira.start_at(cursor) == 5
