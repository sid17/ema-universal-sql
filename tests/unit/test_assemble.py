"""Stage 5b — the envelope contract.

`02-DEFINITION-OF-DONE.md` §4's third non-negotiable lives here: empty, partial
and error must be three distinct shapes a caller can tell apart without reading
prose. So must the rails HLD §9 fixes — freshness is the STALEST contributor,
and `next_cursor` is null whenever `partial`.
"""

import pytest

from src.connectors.errors import FailureMode
from src.connectors.pagination import encode_token
from src.entitlement.engine import EntitlementEngine
from src.execution.assemble import (
    DEFAULT_PAGE_SIZE,
    RESULT_STRATEGY,
    Page,
    decode_cursor,
)
from src.execution.duckdb_pool import DuckDBPool
from src.execution.federation import FederationEngine
from src.models.errors import InvalidQueryError
from src.planner.planner import QueryPlanner
from tests.unit.conftest import CANONICAL_SQL, CLS_DEMO_SQL, GRANTED, persona
from tests.unit.test_entitlement import CLS_REPORTER, RLS_ASSIGNEE, FakePolicyStore

POLICIES = (RLS_ASSIGNEE, CLS_REPORTER)


def make_plan(parser, sql=CANONICAL_SQL, user_id="alice", policies=POLICIES):
    parsed = parser.parse_and_validate(sql, GRANTED)
    entitled = EntitlementEngine(FakePolicyStore(policies)).compile(parsed, persona(user_id))
    return QueryPlanner().plan(entitled)


async def envelope(
    parser,
    adapters,
    assembler,
    sql=CANONICAL_SQL,
    user_id="alice",
    page=None,
    max_staleness_ms=60_000,
    deadline_ms=5000,
    policies=POLICIES,
):
    plan = make_plan(parser, sql=sql, user_id=user_id, policies=policies)
    page = page or Page(offset=0, size=plan.limit or DEFAULT_PAGE_SIZE)
    # A helper-local pool, closed straight after: this helper is imported by
    # `test_assemble_provenance.py` too, and a fixture threaded through both
    # would add a parameter to every caller for no assertion's benefit.
    pool = DuckDBPool()
    try:
        result = await FederationEngine(adapters, deadline_ms=deadline_ms, pool=pool).execute(
            plan, "tenant_acme", max_staleness_ms, row_limit=page.probe_limit
        )
        return await assembler.assemble(
            plan, result, "tenant_acme", "trace-abc", max_staleness_ms, page
        )
    finally:
        pool.close()


# --- columns ----------------------------------------------------------------


async def test_columns_carry_name_type_and_owning_source(parser, adapters, assembler):
    env = await envelope(parser, adapters, assembler)
    by_name = {c.name: c for c in env.columns}
    assert by_name["title"].source == "github"
    assert by_name["key"].source == "jira"
    assert by_name["title"].type == "string"


async def test_a_masked_column_is_flagged(parser, adapters, assembler):
    """`masked=True` is resolved through the projection's INNER column, so
    `MD5(issue.reporter_email)` is still attributed to Jira rather than to
    nothing."""
    env = await envelope(parser, adapters, assembler, sql=CLS_DEMO_SQL)
    masked = [c for c in env.columns if c.masked]
    assert [c.name for c in masked] == ["reporter_email"]
    assert masked[0].source == "jira"


async def test_unmasked_columns_are_not_flagged(parser, adapters, assembler):
    env = await envelope(parser, adapters, assembler, sql=CLS_DEMO_SQL)
    assert [c.name for c in env.columns if not c.masked] == [
        "title",
        "author",
        "key",
        "status",
    ]


async def test_the_raw_email_is_absent_from_the_rows(parser, adapters, assembler):
    """The property that matters, stated about the payload rather than the
    metadata: a `masked` flag on a column whose value leaked is worthless."""
    env = await envelope(parser, adapters, assembler, sql=CLS_DEMO_SQL)
    assert "@acme.com" not in str(env.rows)


# --- GATE: pagination -------------------------------------------------------


def test_a_cursor_round_trips():
    page = decode_cursor(encode_token(RESULT_STRATEGY, 50), 50)
    assert page.offset == 50
    assert page.size == 50


def test_no_cursor_starts_at_zero():
    assert decode_cursor(None, 50) == Page(offset=0, size=50)


def test_a_missing_limit_uses_the_default_page_size():
    assert decode_cursor(None, None).size == DEFAULT_PAGE_SIZE


def test_a_connector_cursor_presented_as_a_result_cursor_is_rejected():
    """The strategy tag earning its keep.

    A GitHub connector cursor reinterpreted as a result offset would return real
    rows for the wrong question — the most expensive kind of wrong answer.
    """
    with pytest.raises(InvalidQueryError):
        decode_cursor(encode_token("cursor", 10), 50)


def test_a_malformed_cursor_is_rejected_not_silently_restarted():
    """Restarting at offset 0 would hand a paging caller a duplicate page with
    no indication anything went wrong — they would blame the data."""
    with pytest.raises(InvalidQueryError) as raised:
        decode_cursor("not-a-cursor", 50)
    assert raised.value.http == 400


def test_the_probe_limit_asks_for_one_extra_row():
    """Asking for exactly the page size cannot distinguish "last page" from
    "full page with more behind it"."""
    assert Page(offset=0, size=2).probe_limit == 3
    assert Page(offset=2, size=2).probe_limit == 5


async def test_a_full_page_returns_a_cursor_and_the_next_window_does_not_overlap(
    parser, adapters, assembler
):
    """**The gate test.** alice has 3 rows; a page size of 2 must page."""
    first = await envelope(parser, adapters, assembler, page=Page(0, 2))
    assert len(first.rows) == 2
    assert first.next_cursor is not None

    second = await envelope(parser, adapters, assembler, page=decode_cursor(first.next_cursor, 2))
    assert len(second.rows) == 1
    assert second.next_cursor is None

    keys = [row[2] for row in first.rows + second.rows]
    assert len(set(keys)) == 3, "pages overlapped or skipped a row"


async def test_the_tiebreaker_makes_the_page_boundary_stable(parser, adapters, assembler):
    """Two of alice's three issues share an `updated` value (ADR-035), and the
    page boundary falls between them. Without `key ASC` the order across the two
    separate queries would not be guaranteed to agree."""
    first = await envelope(parser, adapters, assembler, page=Page(0, 2))
    assert [row[2] for row in first.rows] == ["SUP-12", "SUP-13"]
    second = await envelope(parser, adapters, assembler, page=Page(2, 2))
    assert [row[2] for row in second.rows] == ["SUP-14"]


async def test_the_last_page_returns_no_cursor(parser, adapters, assembler):
    env = await envelope(parser, adapters, assembler)
    assert len(env.rows) == 3
    assert env.next_cursor is None


async def test_cursor_is_null_when_partial(parser, adapters, assembler):
    """**HLD §9 rail.** Paging from an incomplete page would silently skip the
    rows the failed source never contributed — making the omission invisible
    exactly when it matters most."""
    adapters["jira"].fail_next(FailureMode.TIMEOUT)
    env = await envelope(parser, adapters, assembler, page=Page(0, 1))
    assert env.partial is True
    assert env.next_cursor is None
