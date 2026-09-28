"""Stage 4 — the per-source split, the capability check and the projection union.

Two of `02-DEFINITION-OF-DONE.md` §4's three non-negotiables are asserted here.
Both are about the same property: pushdown is an optimization, and getting it
wrong must never change the answer.
"""

import pytest
from sqlglot import exp

from src.entitlement.engine import EntitlementEngine
from src.models.errors import InvalidQueryError
from src.planner.planner import QueryPlanner
from tests.unit.conftest import CANONICAL_SQL, CLS_DEMO_SQL, GRANTED, persona
from tests.unit.test_entitlement import CLS_REPORTER, RLS_ASSIGNEE, FakePolicyStore


def build_plan(parser, sql=CANONICAL_SQL, policies=(RLS_ASSIGNEE, CLS_REPORTER), user=None):
    parsed = parser.parse_and_validate(sql, GRANTED)
    entitled = EntitlementEngine(FakePolicyStore(policies)).compile(
        parsed, user or persona("alice")
    )
    return QueryPlanner().plan(entitled)


@pytest.fixture
def plan(parser):
    return build_plan(parser)


# --- the per-source split ---------------------------------------------------


def test_each_source_gets_only_its_own_predicates(plan):
    assert {p.column for p in plan.sources["pr"].pushed} == {"repo", "state"}
    assert {p.column for p in plan.sources["issue"].pushed} == {"status", "assignee"}


def test_the_rls_predicate_is_pushed_to_the_source(plan):
    """**Non-negotiable #1, at the planner.**

    `assignee = 'alice'` leaves here bound for Jira. That is what makes the
    forbidden rows never-fetched rather than fetched-and-dropped. Measured
    end-to-end, the Jira fetch shrinks 9 -> 3 -> 1 across the personas.
    """
    pushed = {p.column: p.value for p in plan.sources["issue"].pushed}
    assert pushed["assignee"] == "alice"


def test_flatten_recurses_so_rls_injection_does_not_collapse_the_split(parser):
    """**The spike finding, asserted where it would actually bite.**

    `tree.where(append=True)` parenthesizes. With a non-recursive flatten the
    four predicates arrive as ONE multi-table leaf, nothing is pushable, and
    every source is fetched unfiltered — while the query still returns the
    correct rows, because the engine re-applies everything. Correct answers by
    full post-filtering is the one outcome this design may not have.
    """
    plan = build_plan(parser)
    pushed = sum(len(s.pushed) for s in plan.sources.values())
    assert pushed == 4, "the conjunction did not separate; nothing would be pushed"


def test_the_join_condition_is_never_pushed(plan):
    """It touches two tables, so it belongs to no source. This falls out of the
    grouping rather than being special-cased — there is no code path that could
    decide to push half a join to GitHub."""
    pushed_columns = {p.column for s in plan.sources.values() for p in s.pushed}
    assert pushed_columns == {"repo", "state", "status", "assignee"}
    assert "issue_key" not in pushed_columns

    # It is not sitting in `not_pushed` either: a multi-table leaf belongs to no
    # source at all, so it never became a candidate. That is the guard — there
    # is no code path that could decide to push half a join to GitHub.
    for source in plan.sources.values():
        assert source.not_pushed == ()


def test_each_pushed_predicate_carries_its_injection_point(plan):
    """A capability is (predicate support) + (where the value goes). Knowing
    GitHub can filter on `state` is useless without knowing it goes in the query
    string while `repo` goes in the URL path."""
    by_column = {p.column: p for p in plan.sources["pr"].pushed}
    assert by_column["repo"].option.inject_into == "path"
    assert by_column["state"].option.inject_into == "query"


def test_fetch_predicates_are_in_the_adapters_shape(plan):
    """The planner's output must be directly usable as `FetchRequest.predicates`
    — a translation layer between them would be a second place to get the
    operator spelling wrong."""
    assert plan.sources["issue"].fetch_predicates()["assignee"] == ("=", "alice")


# --- the capability check ---------------------------------------------------


def test_an_unsupported_operator_becomes_residual_never_dropped(parser):
    """**The data-leak guard.**

    No connector declares `!=`. A planner that silently dropped it would return
    rows the caller filtered out — and if the predicate were the RLS filter,
    that is a leak rather than a bug. It must stay for the engine.
    """
    plan = build_plan(
        parser,
        sql=(
            "SELECT issue.key, issue.status FROM jira.issues issue "
            "WHERE issue.status != 'Done'"
        ),
        policies=(),
    )
    source = plan.sources["issue"]
    assert source.pushed == ()
    assert len(source.not_pushed) == 1

    # ...and it is still in the tree the engine executes. This is the half that
    # makes "residual" mean something.
    assert "<>" in plan.ast.sql(dialect="duckdb")


def test_an_unsupported_column_becomes_residual(parser):
    """`title` is a real GitHub column but is not filterable — it is not in
    `key_columns`. Supported-to-select is not supported-to-filter."""
    plan = build_plan(
        parser,
        sql=(
            "SELECT pr.title FROM github.pull_requests pr "
            "WHERE pr.repo = 'ema/core' AND pr.title = 'x'"
        ),
        policies=(),
    )
    assert {p.column for p in plan.sources["pr"].pushed} == {"repo"}
    assert len(plan.sources["pr"].not_pushed) == 1


def test_a_range_operator_is_pushed_only_where_it_is_supported(parser):
    """Jira's `updated` takes range operators; nothing on GitHub does. That
    asymmetry is why `supports(column, op)` is a two-argument question."""
    plan = build_plan(
        parser,
        sql=(
            "SELECT issue.key FROM jira.issues issue "
            "WHERE issue.updated > '2026-09-01'"
        ),
        policies=(),
    )
    assert [(p.column, p.op) for p in plan.sources["issue"].pushed] == [("updated", ">")]


def test_a_missing_required_filter_is_a_400_naming_the_column(parser):
    """GitHub's `repo` is a path segment — a fetch without it has no URL to call.
    This is the capability model's `require: required` earning its keep."""
    with pytest.raises(InvalidQueryError) as raised:
        build_plan(
            parser,
            sql="SELECT pr.title FROM github.pull_requests pr WHERE pr.state = 'open'",
            policies=(),
        )
    assert raised.value.detail == "MissingRequiredFilter"
    assert "repo" in str(raised.value)


def test_jira_has_no_required_filter(parser):
    """The other half of the contract: Jira's filters are all optional, so the
    two sources between them prove both requirements rather than just one."""
    plan = build_plan(
        parser, sql="SELECT issue.key FROM jira.issues issue", policies=()
    )
    assert plan.sources["issue"].pushed == ()


# --- GATE: test_projection_union_guard -------------------------------------


def test_projection_union_guard(plan):
    """**Non-negotiable #2.** Fetch = projection ∪ WHERE ∪ ORDER BY ∪ join key.

    Without it the engine's authoritative re-filter would evaluate a predicate
    against a column that was never fetched and drop rows the caller was
    entitled to.
    """
    assert set(plan.sources["pr"].columns_to_fetch) == {
        "title", "author",        # projection
        "repo", "state",          # WHERE
        "issue_key",              # join key
    }
    assert set(plan.sources["issue"].columns_to_fetch) == {
        "key", "status",          # projection + WHERE + join key
        "assignee",               # the injected RLS predicate
        "updated",                # ORDER BY, and NOT projected
    }


def test_an_order_by_column_that_is_not_projected_is_still_fetched(parser):
    """The narrow case the guard exists for, isolated.

    `updated` appears only in ORDER BY. A projection-only fetch would omit it and
    the in-engine sort would then have nothing to sort on.
    """
    plan = build_plan(
        parser,
        sql="SELECT issue.key FROM jira.issues issue ORDER BY issue.updated DESC",
        policies=(),
    )
    assert "updated" in plan.sources["issue"].columns_to_fetch


def test_a_masked_column_is_still_fetched(parser):
    """The union is computed AFTER stage 3, so `MD5(issue.reporter_email)` still
    contributes its column. Masking a value does not stop us needing to read it."""
    plan = build_plan(parser, sql=CLS_DEMO_SQL)
    assert "reporter_email" in plan.sources["issue"].columns_to_fetch


def test_a_dropped_column_is_not_fetched(parser):
    """`drop` is the one mask that is also a column-pushdown saving: the column
    leaves the projection, so the union stops asking for it."""
    plan = build_plan(
        parser, sql=CLS_DEMO_SQL, policies=({**CLS_REPORTER, "mask": "drop"},)
    )
    assert "reporter_email" not in plan.sources["issue"].columns_to_fetch


def test_columns_to_fetch_is_sorted(plan):
    """Sorted so the connector cache key is stable across requests referencing
    the same columns in a different order."""
    for source in plan.sources.values():
        assert list(source.columns_to_fetch) == sorted(source.columns_to_fetch)


def test_every_fetched_column_is_one_the_source_declares(plan):
    """Anything else and Phase 1's adapter rejects the fetch outright."""
    for source in plan.sources.values():
        assert set(source.columns_to_fetch) <= set(source.source.capabilities.columns)


# --- invariant #2a: the engine still holds every predicate ------------------


def test_the_executed_tree_keeps_the_predicates_that_were_pushed(plan):
    """Pushed predicates are applied TWICE — at the source and in the engine.

    That is deliberate and free. The alternative, subtracting pushed predicates
    from the executed SQL, creates a list that has to stay in sync with what
    each connector actually managed to apply — and the day it drifts, rows leak.
    """
    rendered = plan.ast.sql(dialect="duckdb")
    for fragment in ("'ema/core'", "'open'", "'In Progress'", "'alice'"):
        assert fragment in rendered


# --- the total-order tiebreaker (HLD §9) ------------------------------------


def test_the_order_tiebreaker_is_appended(plan):
    """**The assertion that fails if the tiebreaker is removed.**

    The behavioural pagination test needs tied rows AND a sort that reorders
    them; on a twenty-row table DuckDB may well return a stable order anyway, so
    a behavioural test alone could pass with the tiebreaker deleted. This one
    cannot.
    """
    order = plan.ast.args["order"].sql(dialect="duckdb")
    assert order.startswith("ORDER BY")
    assert "updated" in order and "DESC" in order
    assert order.index("updated") < order.index("key")
    assert order.rstrip().endswith("ASC")


def test_the_tiebreaker_is_not_added_twice(parser):
    """A query that already orders by the join key needs nothing appended."""
    plan = build_plan(
        parser,
        sql=(
            "SELECT issue.key FROM jira.issues issue "
            "ORDER BY issue.updated DESC, issue.key ASC"
        ),
        policies=(),
    )
    keys = [
        c.name
        for term in plan.ast.args["order"].expressions
        for c in term.find_all(exp.Column)
    ]
    assert keys.count("key") == 1


def test_a_query_with_no_order_by_gets_a_deterministic_one(parser):
    """A cursor over an unordered result is not a cursor.

    With no ORDER BY and no join key, the fallback orders by every projected
    column — a total order over the RESULT, which is the only thing a cursor has
    to be stable across. Two rows agreeing on every visible column are
    indistinguishable, so their relative order cannot skip or duplicate anything
    a caller could notice.
    """
    plan = build_plan(
        parser, sql="SELECT issue.key, issue.status FROM jira.issues issue", policies=()
    )
    order = plan.ast.args.get("order")
    assert order is not None
    assert {c.name for term in order.expressions for c in term.find_all(exp.Column)} == {
        "key",
        "status",
    }


def test_the_fallback_ordering_is_appended_after_the_callers_own(parser):
    """The caller's sort must stay primary; the tiebreaker only breaks ties."""
    plan = build_plan(
        parser,
        sql=(
            "SELECT issue.key, issue.status FROM jira.issues issue "
            "ORDER BY issue.status DESC"
        ),
        policies=(),
    )
    terms = plan.ast.args["order"].expressions
    assert terms[0].this.name == "status"
    assert terms[0].args.get("desc") is True
    assert {c.name for term in terms for c in term.find_all(exp.Column)} == {"key", "status"}


def test_the_limit_is_carried_but_never_pushed(plan):
    """LIMIT applies AFTER the join. Pushing `LIMIT 50` to each source would
    take the first 50 rows of each side and join those — which is a different
    query, and usually a wrong one."""
    assert plan.limit == 50
    for source in plan.sources.values():
        assert all(p.column != "limit" for p in source.pushed)
