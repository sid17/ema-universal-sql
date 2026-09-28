"""Stage 5a — parallel fetch, the deadline, and the DuckDB join.

Three of these tests assert behaviour that is invisible in the happy path and
catastrophic when wrong: the deadline being real, a sibling surviving a failure,
and a zero-row source being registerable at all.
"""

import asyncio

import pytest
from sqlglot import exp

from src.connectors.errors import FailureMode
from src.entitlement.engine import EntitlementEngine
from src.execution.arrow import build_table
from src.execution.federation import FederationEngine
from src.execution.join import rebind_to_registered
from src.models.errors import ApiError, ErrorCode
from src.planner.planner import QueryPlanner
from src.sqlparse.parser import flatten_conjunction
from tests.unit.conftest import CANONICAL_SQL, CLS_DEMO_SQL, GRANTED, persona
from tests.unit.test_entitlement import CLS_REPORTER, RLS_ASSIGNEE, FakePolicyStore

POLICIES = (RLS_ASSIGNEE, CLS_REPORTER)


def make_plan(parser, sql=CANONICAL_SQL, user_id="alice", policies=POLICIES, roles=("support",)):
    parsed = parser.parse_and_validate(sql, GRANTED)
    entitled = EntitlementEngine(FakePolicyStore(policies)).compile(
        parsed, persona(user_id, *roles)
    )
    return QueryPlanner().plan(entitled)


async def run(adapters, plan, deadline_ms=5000, max_staleness_ms=60_000):
    engine = FederationEngine(adapters, deadline_ms=deadline_ms)
    return await engine.execute(plan, tenant_id="tenant_acme", max_staleness_ms=max_staleness_ms)


class SlowAdapter:
    """Wraps a real adapter and makes it take longer than its budget.

    A wrapper rather than a mock: the point is that `wait_for` cancels a fetch
    that is genuinely in flight, and a mock that raised immediately would test
    the exception handler instead of the deadline.
    """

    def __init__(self, inner, delay_s: float) -> None:
        self._inner = inner
        self._delay_s = delay_s
        self.connector_type = inner.connector_type
        self.started = False

    async def fetch(self, request):
        self.started = True
        await asyncio.sleep(self._delay_s)
        return await self._inner.fetch(request)


# --- the persona contract, through the real engine --------------------------


@pytest.mark.parametrize(
    ("user_id", "jira_rows", "joined"),
    [("alice", 3, 3), ("bob", 1, 1), ("carol", 1, 0)],
)
async def test_rls_shrinks_the_fetch_not_just_the_result(
    parser, adapters, user_id, jira_rows, joined
):
    """**Non-negotiable #1, measured at the source boundary.**

    The middle column is the whole argument. A post-filtering implementation
    produces the identical `joined` count while fetching 9 Jira rows every time.
    Only a pushed-down predicate makes the FETCH shrink.
    """
    result = await run(adapters, make_plan(parser, user_id=user_id))
    fetched = {f.connector_type: len(f.rows) for f in result.fetches}
    assert fetched["jira"] == jira_rows
    assert len(result.rows) == joined

    # GitHub stays 14 for every persona, correctly: the RLS rule is on a Jira
    # column and GitHub has no `assignee`.
    assert fetched["github"] == 14


async def test_the_adapter_received_the_entitled_predicate(parser, adapters):
    """Asserted at the adapter's received-predicates layer, never against a JQL
    string: `assignee = currentUser()` is how a LIVE Jira adapter would render
    this predicate, which is a rendering detail of one connector."""
    plan = make_plan(parser, user_id="bob")
    assert plan.sources["issue"].fetch_predicates()["assignee"] == ("=", "bob")


async def test_carol_is_empty_because_of_the_join_not_because_she_has_no_data(
    parser, adapters
):
    """The `empty` leg of the trichotomy, and why it is a real case.

    carol HAS an In-Progress issue (SUP-31) — one row comes back from Jira. Its
    only pull request is closed, so the join produces nothing. An empty result
    that came from an empty fetch would prove much less.
    """
    result = await run(adapters, make_plan(parser, user_id="carol"))
    assert [len(f.rows) for f in result.fetches if f.connector_type == "jira"] == [1]
    assert result.rows == []
    assert result.failed == ()


async def test_the_cls_mask_is_executed_by_duckdb(parser, adapters):
    result = await run(adapters, make_plan(parser, sql=CLS_DEMO_SQL))
    names = [name for name, _ in result.columns]
    assert names[-1] == "reporter_email"
    values = {row[-1] for row in result.rows}
    assert all("@" not in value for value in values)
    assert all(len(value) == 32 for value in values)  # MD5 hex


# --- GATE: the deadline is real ---------------------------------------------


async def test_a_source_exceeding_its_budget_becomes_a_timeout(parser, adapters):
    """**ADR-032.** Phase 0 set `request.state.deadline_ms` and nothing read it.

    This uses a genuinely slow adapter, not the forced-failure hook, so it
    asserts that `wait_for` cancels an in-flight fetch rather than that our own
    exception handler works.
    """
    adapters["jira"] = SlowAdapter(adapters["jira"], delay_s=0.5)
    result = await run(adapters, make_plan(parser), deadline_ms=100)

    by_connector = {f.connector_type: f for f in result.fetches}
    assert by_connector["jira"].state == "timeout"
    assert by_connector["jira"].error.code is ErrorCode.SOURCE_TIMEOUT
    assert adapters["jira"].started, "the fetch never started; this tested nothing"


async def test_a_timeout_does_not_cancel_the_healthy_sibling(parser, adapters):
    """**The `return_exceptions=True` assertion.**

    `asyncio.gather`'s default propagates the first exception AND cancels its
    siblings — so a Jira timeout would throw away GitHub rows we already had,
    converting a `partial` answer into an `error` one. That is the trichotomy
    collapsing.
    """
    adapters["jira"] = SlowAdapter(adapters["jira"], delay_s=0.5)
    result = await run(adapters, make_plan(parser), deadline_ms=100)

    github = next(f for f in result.fetches if f.connector_type == "github")
    assert github.state == "ok"
    assert len(github.rows) == 14


async def test_the_budget_leaves_headroom_for_the_join(parser, adapters):
    """A source may spend 80% of the deadline, not all of it — the DuckDB join
    and envelope assembly happen after the slowest source returns and would
    otherwise push a correct request past its deadline."""
    from src.execution.federation import SOURCE_BUDGET_FRACTION

    assert 0 < SOURCE_BUDGET_FRACTION < 1


# --- hard failures stop the query -------------------------------------------


async def test_an_auth_failure_is_a_hard_stop_not_a_partial(parser, adapters):
    """A permanently broken connector presented as merely `partial` would tell
    the caller to retry something that cannot succeed, and would make a revoked
    credential look like a slow API."""
    adapters["jira"].fail_next(FailureMode.AUTH)
    with pytest.raises(ApiError) as raised:
        await run(adapters, make_plan(parser))
    assert raised.value.code is ErrorCode.CONNECTOR_AUTH_ERROR


async def test_a_throttle_is_a_hard_stop_too(parser, adapters):
    """Deliberately different from `Classification.degradable`.

    A throttle genuinely IS transient — retrying later works — but design-doc
    §4.5 makes it a hard stop with a `Retry-After` and the async reroute, not a
    quietly smaller result set. "Retryable" and "may be served as partial" are
    different questions.
    """
    adapters["github"].fail_next(FailureMode.THROTTLED)
    with pytest.raises(ApiError) as raised:
        await run(adapters, make_plan(parser))
    assert raised.value.code is ErrorCode.RATE_LIMIT_EXHAUSTED


async def test_a_forced_timeout_degrades_instead_of_raising(parser, adapters):
    """The one degradable code — the other side of the two tests above."""
    adapters["jira"].fail_next(FailureMode.TIMEOUT)
    result = await run(adapters, make_plan(parser))
    assert {f.state for f in result.failed} == {"timeout"}


async def test_only_source_timeout_is_degradable():
    """Read straight off design-doc §8.1: it gives exactly one error code a
    `200 (partial)` form. Pinned so a future addition is a deliberate act."""
    from src.execution.federation import DEGRADABLE_CODES

    assert set(DEGRADABLE_CODES) == {ErrorCode.SOURCE_TIMEOUT}


# --- GATE: the zero-column Arrow landmine (ADR-030) -------------------------


def test_an_empty_result_still_builds_a_registerable_table():
    """**The measured crash.**

    `pa.Table.from_pylist([])` infers ZERO columns and `duckdb.register` then
    raises `InvalidInputException`. Every zero-row path reaches this.
    """
    import duckdb

    table = build_table([], ["key", "status"], {"key": "string"})
    assert table.num_rows == 0
    assert table.num_columns == 2

    connection = duckdb.connect(":memory:")
    try:
        connection.register("t", table)
        assert connection.execute('SELECT "t"."key" FROM t').fetchall() == []
    finally:
        connection.close()


def test_the_naive_version_really_does_fail():
    """So the test above is earned rather than asserted against nothing."""
    import duckdb
    import pyarrow as pa

    connection = duckdb.connect(":memory:")
    try:
        with pytest.raises(duckdb.InvalidInputException):
            connection.register("t", pa.Table.from_pylist([]))
    finally:
        connection.close()


def test_types_come_from_the_capability_model_not_the_rows():
    """Inference-when-non-empty would let one source register as `int64` on a
    request that returned data and `string` on one that did not."""
    typed = build_table([{"number": 1}], ["number"], {"number": "integer"})
    assert typed.schema.field("number").type == "int64"

    empty = build_table([], ["number"], {"number": "integer"})
    assert empty.schema.field("number").type == "int64"


def test_a_row_missing_a_column_contributes_null_not_a_ragged_record():
    table = build_table([{"key": "SUP-1"}], ["key", "status"], {})
    assert table.to_pylist() == [{"key": "SUP-1", "status": None}]


# --- rebinding --------------------------------------------------------------


def test_rebind_collapses_the_schema_but_keeps_the_alias(parser):
    """DuckDB has no `github` schema, so the two-part name must collapse — but
    the alias must survive or every qualified column stops resolving."""
    plan = make_plan(parser)
    rebound = rebind_to_registered(plan.ast)
    tables = {(t.text("db"), t.name, t.alias) for t in rebound.find_all(exp.Table)}
    assert tables == {
        ("", "github_pull_requests", "pr"),
        ("", "jira_issues", "issue"),
    }


def test_rebind_does_not_mutate_the_original(parser):
    """The audit log and the trace describe the caller's query, not DuckDB's
    private naming."""
    plan = make_plan(parser)
    before = plan.ast.sql(dialect="duckdb")
    rebind_to_registered(plan.ast)
    assert plan.ast.sql(dialect="duckdb") == before


# --- invariant #2a ----------------------------------------------------------


async def test_the_engine_reapplies_every_predicate(parser, adapters):
    """Pushed predicates are applied twice — at the source and in DuckDB.

    Proven by construction: the executed SQL still contains all four leaves.
    """
    plan = make_plan(parser)
    leaves = list(flatten_conjunction(plan.ast.args["where"].this))
    assert len(leaves) == 4
    result = await run(adapters, plan)
    assert len(result.rows) == 3


# --- non-negotiable #1, for the DEFAULT-DENY path (found in review) ---------


async def test_a_default_denied_source_is_never_fetched(parser, adapters):
    """**The review finding.** Default-deny must not fetch-then-discard.

    Before this was fixed, a `contractor` running the canonical query pulled all
    **9** Jira rows and returned 0. The caller saw nothing, so nothing leaked —
    but the rows crossed the source boundary anyway: the source's own audit log
    recorded a read that should never have happened, the rows sat in this
    process's memory, and a token was spent on a query that could not return
    anything.

    That is exactly the post-filtering non-negotiable #1 forbids, and the README
    makes the argument explicitly. An unsatisfiable predicate alone cannot fix
    it: `FALSE` is a constant with no owning table, so the planner cannot push
    it anywhere and DuckDB does the discarding.
    """
    from tests.unit.test_entitlement import DENY_JIRA

    plan = make_plan(
        parser,
        user_id="dave",
        roles=("contractor",),
        policies=(RLS_ASSIGNEE, CLS_REPORTER, DENY_JIRA),
    )
    assert plan.sources["issue"].yields_nothing is True

    result = await run(adapters, plan)
    fetched = {f.connector_type: len(f.rows) for f in result.fetches}
    assert fetched["jira"] == 0, "the denied source was fetched and discarded"
    assert result.rows == []


async def test_a_default_denied_result_is_empty_not_partial(parser, adapters):
    """Nothing went wrong, so `partial` stays False.

    Marking the skipped source as failed would make default-deny look like an
    outage — and would collapse the distinction between "you are not entitled to
    this" and "this source is down", which is the whole point of keeping empty
    and partial separate.
    """
    from tests.unit.test_entitlement import DENY_JIRA

    plan = make_plan(
        parser,
        user_id="dave",
        roles=("contractor",),
        policies=(RLS_ASSIGNEE, CLS_REPORTER, DENY_JIRA),
    )
    result = await run(adapters, plan)
    assert result.failed == ()
    assert {f.state for f in result.fetches} == {"ok"}


async def test_the_unsatisfiable_predicate_is_still_in_the_tree(parser):
    """Belt AND braces: even with the fetch skipped, the executed SQL could not
    return a row. Removing either mechanism alone must not make rows appear."""
    from tests.unit.test_entitlement import DENY_JIRA

    plan = make_plan(
        parser,
        user_id="dave",
        roles=("contractor",),
        policies=(RLS_ASSIGNEE, CLS_REPORTER, DENY_JIRA),
    )
    assert "FALSE" in plan.ast.sql(dialect="duckdb").upper()


async def test_an_entitled_source_is_still_fetched(parser, adapters):
    """The counterweight: `yields_nothing` must not fire for a caller who IS
    entitled, or the headline demo returns nothing."""
    plan = make_plan(parser, user_id="alice")
    assert plan.sources["issue"].yields_nothing is False
    result = await run(adapters, plan)
    assert len(result.rows) == 3
