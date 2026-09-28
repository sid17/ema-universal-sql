"""Stage 5b — the envelope contract.

`02-DEFINITION-OF-DONE.md` §4's third non-negotiable lives here: empty, partial
and error must be three distinct shapes a caller can tell apart without reading
prose. So must the rails HLD §9 fixes — freshness is the STALEST contributor,
and `next_cursor` is null whenever `partial`.
"""


import pytest

from src.connectors.base import AdapterResponse
from src.connectors.errors import FailureMode
from src.connectors.pagination import encode_token
from src.entitlement.engine import EntitlementEngine
from src.execution.assemble import (
    DEFAULT_PAGE_SIZE,
    RESULT_STRATEGY,
    Page,
    ResultAssembler,
    decode_cursor,
)
from src.execution.federation import FederationEngine, FederationResult, SourceFetch
from src.models.errors import ApiError, InvalidQueryError
from src.planner.planner import QueryPlanner
from tests.unit.conftest import CANONICAL_SQL, CLS_DEMO_SQL, GRANTED, persona
from tests.unit.test_entitlement import CLS_REPORTER, RLS_ASSIGNEE, FakePolicyStore

POLICIES = (RLS_ASSIGNEE, CLS_REPORTER)


def make_plan(parser, sql=CANONICAL_SQL, user_id="alice", policies=POLICIES):
    parsed = parser.parse_and_validate(sql, GRANTED)
    entitled = EntitlementEngine(FakePolicyStore(policies)).compile(parsed, persona(user_id))
    return QueryPlanner().plan(entitled)


@pytest.fixture
def assembler(limiter, control_plane, fake_clock) -> ResultAssembler:
    """On the SAME injected clock as the cache the rows came from.

    Reading wall-clock time here while `fetched_at` came from `FakeClock` would
    make every fixture look hours stale and fire spurious STALE_DATA warnings.
    """
    return ResultAssembler(limiter, control_plane, fake_clock)


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
    result = await FederationEngine(adapters, deadline_ms=deadline_ms).execute(
        plan, "tenant_acme", max_staleness_ms, row_limit=page.probe_limit
    )
    return await assembler.assemble(
        plan, result, "tenant_acme", "trace-abc", max_staleness_ms, page
    )


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
        "title", "author", "key", "status",
    ]


async def test_the_raw_email_is_absent_from_the_rows(parser, adapters, assembler):
    """The property that matters, stated about the payload rather than the
    metadata: a `masked` flag on a column whose value leaked is worthless."""
    env = await envelope(parser, adapters, assembler, sql=CLS_DEMO_SQL)
    assert "@acme.com" not in str(env.rows)


# --- freshness (HLD §9) -----------------------------------------------------


async def test_freshness_reports_the_stalest_contributor(parser, assembler, fake_clock):
    """Not the average and not the newest. A caller comparing it to their
    tolerance is asking "is ALL of this fresh enough", which is the only safe
    question about a joined result."""
    plan = make_plan(parser)
    # Stamps derived from the SAME injected clock the assembler reads, in the
    # seconds unit `AdapterResponse.fetched_at` uses.
    now_s = fake_clock() / 1000.0
    old = now_s - 30
    new = now_s - 1
    fetches = tuple(
        SourceFetch(
            alias=alias,
            connector_type=plan.sources[alias].connector_type,
            plan=plan.sources[alias],
            elapsed_ms=1.0,
            response=AdapterResponse(rows=[], fetched_at=stamp, served="live"),
        )
        for alias, stamp in (("pr", new), ("issue", old))
    )
    result = FederationResult(rows=[], columns=(), fetches=fetches)
    env = await assembler.assemble(
        plan, result, "tenant_acme", "t", 60_000, Page(0, 50)
    )
    assert 29_000 < env.freshness_ms < 31_000  # the OLD one, not the new one


async def test_freshness_is_none_when_nothing_was_fetched(parser, assembler, fake_clock):
    """Honest: there is no data whose age could be reported. Zero would claim
    perfect freshness for an answer with no contributors."""
    plan = make_plan(parser)
    result = FederationResult(rows=[], columns=(), fetches=())
    env = await assembler.assemble(
        plan, result, "tenant_acme", "t", 60_000, Page(0, 50)
    )
    assert env.freshness_ms is None


# --- GATE: the three outcomes stay distinct ---------------------------------


async def test_a_successful_query_is_complete_and_not_partial(
    parser, adapters, assembler
):
    env = await envelope(parser, adapters, assembler)
    assert len(env.rows) == 3
    assert env.partial is False
    assert env.join_status == "complete"
    assert env.warnings == []


async def test_empty_is_a_correct_answer_not_a_degraded_one(
    parser, adapters, assembler
):
    """**The `empty` leg.** carol ran fine and matched nothing."""
    env = await envelope(parser, adapters, assembler, user_id="carol")
    assert env.rows == []
    assert env.partial is False
    assert env.join_status == "complete"
    assert env.warnings == []


async def test_partial_is_visibly_different_from_empty(
    parser, adapters, assembler
):
    """**The `partial` leg.** Rows present, a side missing, and said so."""
    adapters["jira"].fail_next(FailureMode.TIMEOUT)
    env = await envelope(parser, adapters, assembler)
    assert env.partial is True
    assert env.join_status == "incomplete"
    assert [w["code"] for w in env.warnings] == ["SOURCE_TIMEOUT"]
    assert env.warnings[0]["connector"] == "jira"


async def test_a_timed_out_join_never_passes_the_unjoined_side_off_as_joined(
    parser, adapters, assembler
):
    """The sharpest honesty requirement in the phase file.

    GitHub returned 14 rows. If those were handed back as "the joined answer",
    a caller would receive PRs with no issue attached and no way to tell.
    """
    adapters["jira"].fail_next(FailureMode.TIMEOUT)
    env = await envelope(parser, adapters, assembler)
    assert len(env.rows) < 14
    assert env.join_status == "incomplete"
    github = [s for s in env.sources if s.connector == "github"][0]
    assert github.state == "ok"


async def test_state_and_served_are_reported_separately(
    parser, adapters, assembler
):
    """`state` is how the fetch ended; `served` is where the rows came from — so
    a timeout still answered from cache is distinguishable from one that
    returned nothing."""
    env = await envelope(parser, adapters, assembler)
    assert {s.served for s in env.sources} == {"live"}
    assert {s.state for s in env.sources} == {"ok"}


async def test_error_is_the_third_shape(parser, adapters, assembler):
    """**The `error` leg.** No envelope at all — it raises before assembly."""
    adapters["jira"].fail_next(FailureMode.AUTH)
    with pytest.raises(ApiError):
        await envelope(parser, adapters, assembler)


# --- join_status ------------------------------------------------------------


async def test_a_query_with_no_join_reports_na(
    parser, adapters, assembler
):
    """`n/a` is not a hedge: "was the join complete" has no answer for a query
    that did not join."""
    env = await envelope(
        parser,
        adapters,
        assembler,
        sql="SELECT issue.key, issue.status FROM jira.issues issue",
        policies=(),
    )
    assert env.join_status == "n/a"


# --- budgets ----------------------------------------------------------------


async def test_rate_limit_status_is_reported_per_connector(
    parser, adapters, assembler
):
    env = await envelope(parser, adapters, assembler)
    assert set(env.rate_limit_status) == {"github", "jira"}
    assert env.rate_limit_status["github"].throttled is False
    assert env.rate_limit_status["github"].remaining > 0


async def test_reading_the_budget_does_not_spend_a_token(
    parser, adapters, assembler
):
    """`remaining()` uses the same Lua as `consume()` with amount=0, so the
    refill arithmetic has exactly one implementation — and reporting the budget
    cannot cost one."""
    env = await envelope(parser, adapters, assembler)
    before = env.rate_limit_status["github"].remaining
    again = await envelope(parser, adapters, assembler)
    # The second query is served from cache, so no token is spent either.
    assert again.rate_limit_status["github"].remaining >= before


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

    second = await envelope(
        parser, adapters, assembler, page=decode_cursor(first.next_cursor, 2)
    )
    assert len(second.rows) == 1
    assert second.next_cursor is None

    keys = [row[2] for row in first.rows + second.rows]
    assert len(set(keys)) == 3, "pages overlapped or skipped a row"


async def test_the_tiebreaker_makes_the_page_boundary_stable(
    parser, adapters, assembler
):
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


# --- stats ------------------------------------------------------------------


async def test_connector_ms_covers_only_live_calls(parser, adapters, assembler):
    """A cache hit costing 0.4ms reported as connector time would make the
    Phase 4 trace claim a source was fast when it was never called. The HLD's
    own envelope example omits Jira for exactly this reason."""
    first = await envelope(parser, adapters, assembler)
    assert set(first.stats["connector_ms"]) == {"github", "jira"}

    cached = await envelope(parser, adapters, assembler)
    assert cached.stats["connector_ms"] == {}
    assert {s.served for s in cached.sources} == {"cache"}


async def test_stage_timings_are_carried_through(parser, adapters, assembler):
    plan = make_plan(parser)
    result = FederationResult(rows=[], columns=(), fetches=())
    env = await assembler.assemble(
        plan, result, "tenant_acme", "t", 60_000, Page(0, 50), stats={"parse_ms": 1.5}
    )
    assert env.stats["parse_ms"] == 1.5


async def test_the_trace_id_is_carried_verbatim(parser, adapters, assembler):
    env = await envelope(parser, adapters, assembler)
    assert env.trace_id == "trace-abc"


# --- STALE_DATA -------------------------------------------------------------


async def test_a_stale_answer_warns_but_still_returns_rows(
    parser, assembler, fake_clock
):
    """STALE_DATA rides back on a 200 (design-doc §8.1): the answer is usable
    but caveated, and failing the query would throw away good rows."""
    plan = make_plan(parser)
    fetches = (
        SourceFetch(
            alias="issue",
            connector_type="jira",
            plan=plan.sources["issue"],
            elapsed_ms=1.0,
            response=AdapterResponse(
                rows=[], fetched_at=(fake_clock() / 1000.0) - 600, served="cache"
            ),
        ),
    )
    result = FederationResult(rows=[["a"]], columns=(("k", "string"),), fetches=fetches)
    env = await assembler.assemble(
        plan, result, "tenant_acme", "t", 1000, Page(0, 50)
    )
    assert [w["code"] for w in env.warnings] == ["STALE_DATA"]
    assert env.rows == [["a"]]
    assert env.partial is False
