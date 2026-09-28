"""The supported-SQL subset, asserted construct by construct.

Each rejection names the sqlglot node it is really about, so the test fails with
"expected Star, got Column" rather than "expected an exception" — which is the
difference between a five-second fix and a debugging session.
"""

import pytest
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

from src.models.errors import InvalidQueryError
from src.sqlparse.whitelist import ALLOWED_NODES, reject_unsupported
from tests.unit.conftest import CANONICAL_SQL, CLS_DEMO_SQL, build_catalog


def _raw(sql: str) -> exp.Expression:
    """Parse WITHOUT qualifying — what the whitelist actually sees."""
    return sqlglot.parse_one(sql, read="duckdb")


# --- what must be accepted --------------------------------------------------


@pytest.mark.parametrize("sql", [CANONICAL_SQL, CLS_DEMO_SQL])
def test_the_locked_queries_are_accepted(sql):
    """If the whitelist rejects the canonical query, the whitelist is wrong.

    This is the assertion that would have caught the phase file's original
    sketch, which omitted `Identifier`, `TableAlias` and `Ordered` — all three
    are structural nodes sqlglot always emits.
    """
    reject_unsupported(_raw(sql))


def test_the_canonical_query_is_accepted_after_qualify_too():
    """The whitelist runs pre-qualify, but must not reject the post-qualify tree.

    Not redundant: `qualify` REWRITES the tree (it adds `Alias` wrappers and
    `Identifier` nodes). A whitelist that only tolerated the raw shape would
    block any future need to re-validate, and the divergence would be invisible
    until then.
    """
    tree = qualify(_raw(CANONICAL_SQL), schema=build_catalog().qualify_schema(), dialect="duckdb")
    reject_unsupported(tree)


@pytest.mark.parametrize(
    "where",
    [
        "pr.state = 'open'",
        "pr.state != 'closed'",
        "issue.updated > '2026-09-01'",
        "issue.updated >= '2026-09-01'",
        "issue.updated < '2026-10-01'",
        "issue.updated <= '2026-10-01'",
        "pr.state = 'open' AND pr.repo = 'ema/core'",
        "pr.state = 'open' OR pr.state = 'closed'",
        "(pr.state = 'open')",
    ],
)
def test_every_supported_operator_is_accepted(where):
    """The operator set is exactly what some connector can push or the engine
    can re-apply — no more (LAW 5) and no less."""
    reject_unsupported(
        _raw(f"SELECT pr.title FROM github.pull_requests pr JOIN jira.issues issue "
             f"ON pr.issue_key = issue.key WHERE {where}")
    )


# --- what must be rejected, each by the node it really is -------------------


@pytest.mark.parametrize(
    ("sql", "node"),
    [
        ("SELECT * FROM jira.issues issue", "Star"),
        ("SELECT issue.* FROM jira.issues issue", "Star"),
        (
            "SELECT pr.title FROM github.pull_requests pr "
            "WHERE pr.issue_key IN (SELECT i.key FROM jira.issues i)",
            "In",
        ),
        (
            "SELECT pr.title FROM github.pull_requests pr "
            "WHERE pr.repo = (SELECT i.key FROM jira.issues i)",
            "Subquery",
        ),
        ("SELECT COUNT(pr.title) FROM github.pull_requests pr", "Count"),
        ("SELECT MAX(pr.title) FROM github.pull_requests pr", "Max"),
        (
            "SELECT pr.title FROM github.pull_requests pr "
            "UNION SELECT i.key FROM jira.issues i",
            "Union",
        ),
        ("SELECT pr.title FROM github.pull_requests pr WHERE pr.repo LIKE 'ema%'", "Like"),
        ("SELECT pr.repo FROM github.pull_requests pr GROUP BY pr.repo", "Group"),
    ],
)
def test_unsupported_constructs_are_rejected_by_name(sql, node):
    with pytest.raises(InvalidQueryError) as raised:
        reject_unsupported(_raw(sql))
    assert raised.value.detail == node
    assert raised.value.http == 400


def test_select_star_is_rejected_with_an_actionable_reason():
    """`SELECT *` is the one rejection a caller is most likely to hit, and the
    reason is a real engineering one — it defeats projection pushdown — so the
    message says that rather than "unsupported node type"."""
    with pytest.raises(InvalidQueryError) as raised:
        reject_unsupported(_raw("SELECT * FROM jira.issues issue"))
    message = str(raised.value)
    assert "name the columns" in message
    assert "every column" in message


def test_a_function_call_is_rejected_generically():
    """There are hundreds of function nodes; the hint is derived, not enumerated."""
    with pytest.raises(InvalidQueryError) as raised:
        reject_unsupported(_raw("SELECT UPPER(pr.title) FROM github.pull_requests pr"))
    assert "is not supported" in str(raised.value)


def test_only_the_first_problem_is_reported():
    """A query with three problems still gets fixed one at a time."""
    with pytest.raises(InvalidQueryError) as raised:
        reject_unsupported(_raw("SELECT COUNT(*) FROM github.pull_requests pr GROUP BY pr.repo"))
    assert raised.value.detail is not None


# --- the set itself ---------------------------------------------------------


def test_the_masking_function_is_not_in_the_whitelist():
    """The CLS `MD5` rewrite would fail this whitelist — which is precisely why
    stage 3 runs AFTER it and the engine's own nodes are never re-validated.

    Asserting it keeps the comment in `whitelist.py` honest: if `Anonymous`/`MD5`
    were ever added to the allowed set, a caller could hand-write the masking
    function and the ordering note would silently stop being load-bearing.
    """
    assert exp.MD5 not in ALLOWED_NODES

    # No *callable* function is allowed. `And`/`Or` are `exp.Func` subclasses in
    # sqlglot (they are `Connector`s that happen to inherit it), so a bare
    # `issubclass(node, exp.Func)` check would fail on the boolean operators the
    # subset genuinely needs. Excluding them by name keeps the assertion about
    # what it is really about: a caller cannot write `MD5(...)` or any other call.
    connectors = {exp.And, exp.Or}
    callables = {n for n in ALLOWED_NODES if issubclass(n, exp.Func)} - connectors
    assert callables == set()


def test_no_write_or_ddl_node_is_allowed():
    """Belt to `into=exp.Select`'s braces — two independent reasons a write fails."""
    for node in (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Command):
        assert node not in ALLOWED_NODES
