"""Regression test for the sqlglot/DuckDB techniques Phase 2 is built on.

Promoted from `spike/ast_spike.py` (Phase 0 Task 0), which passed 14/14 before
any infra existed. Kept rather than deleted because the bug it caught is
*invisible to result-based assertions*: a single `where.this.flatten()` returns
correct rows while silently pushing **zero** predicates to GitHub, which would
falsify the design doc's central claim without failing a single row comparison.

Infra-free by construction — the pre-commit hook runs `pytest -q tests/unit`.
"""

import pytest

from tests.unit.ast_fixtures import (
    CANONICAL_SQL,
    CLS_DEMO_SQL,
    GITHUB_PULL_REQUESTS,
    JIRA_ISSUES,
)
from tests.unit.ast_techniques import (
    add_order_tiebreaker,
    alias_to_source,
    apply_cls_mask,
    execute,
    fetch,
    inject_rls,
    parse_and_qualify,
    projection_union,
    rebind_to_registered,
    split_predicates,
)

# --------------------------------------------------------------------------


def entitled_tree(sql: str, user: str, *, mask: bool = False):
    """parse -> qualify -> inject RLS -> (optionally) mask -> add tiebreaker."""
    tree = parse_and_qualify(sql)
    aliases = alias_to_source(tree)
    jira_alias = next(a for a, s in aliases.items() if s == ("jira", "issues"))
    tree = inject_rls(tree, jira_alias, "assignee", user)
    if mask:
        assert apply_cls_mask(tree, jira_alias, "reporter_email")
    tree = add_order_tiebreaker(tree, jira_alias, "key")
    return tree, aliases, jira_alias


def run_for(sql: str, user: str, *, mask: bool = False):
    tree, aliases, _ = entitled_tree(sql, user, mask=mask)
    return execute(rebind_to_registered(tree), fetch(tree, aliases))


# --------------------------------------------------------------------------
# Gate 1 — RLS is visible in the row count (HLD §6)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("user", "expected"), [("alice", 3), ("bob", 1), ("carol", 0)])
def test_rls_row_counts(user, expected):
    """alice 3 / bob 1 / carol 0. bob is deliberately 1 and not 0 — a count that
    collapses to zero is indistinguishable from a broken query."""
    assert run_for(CANONICAL_SQL, user).num_rows == expected


# --------------------------------------------------------------------------
# Gate 2 — the per-source predicate split (the bug this file exists for)
# --------------------------------------------------------------------------


def test_predicate_split_pushes_to_both_sources():
    tree, aliases, _ = entitled_tree(CANONICAL_SQL, "alice")
    pushable, residual = split_predicates(tree, aliases)
    by_source = {aliases[a][0]: sorted(p) for a, p in pushable.items()}

    assert len(by_source["github"]) == 2, (
        "GitHub lost its predicates — this is the Card 1 flatten() bug: "
        "tree.where(append=True) parenthesizes the existing WHERE, so a "
        "non-recursive flatten yields the whole AND as one unpushable leaf."
    )
    assert len(by_source["jira"]) == 2
    assert any("assignee" in p for p in by_source["jira"]), "RLS never reached Jira"
    assert residual == [], "the join lives in ON, so nothing should be residual"


def test_cross_source_predicate_is_not_pushable():
    """A leaf touching two tables must fall out as residual — that is the guard."""
    tree = parse_and_qualify(
        CANONICAL_SQL.replace("issue.status = 'In Progress'", "pr.author = issue.assignee")
    )
    _, residual = split_predicates(tree, alias_to_source(tree))
    assert len(residual) == 1


# --------------------------------------------------------------------------
# Gate 3 — the projection-union guard (DoD §4 non-negotiable #2)
# --------------------------------------------------------------------------


def test_projection_union_widens_the_fetch():
    """fetch = projection UNION every WHERE/ORDER BY/JOIN-key column, or the
    engine's authoritative re-filter drops rows it cannot see."""
    tree, aliases, jira_alias = entitled_tree(CANONICAL_SQL, "alice")
    needed = projection_union(tree, aliases)
    gh_alias = next(a for a, s in aliases.items() if s == ("github", "pull_requests"))

    assert sorted(needed[gh_alias]) == ["author", "issue_key", "repo", "state", "title"]
    assert {"assignee", "updated"} <= needed[jira_alias], (
        "Jira must fetch assignee and updated even though it projects neither"
    )


def test_entitled_rows_are_never_fetched():
    """The rows RLS excludes must not cross the adapter boundary at all."""
    tree, aliases, _ = entitled_tree(CANONICAL_SQL, "alice")
    fetched = fetch(tree, aliases)
    assert fetched["github_pull_requests"].num_rows < len(GITHUB_PULL_REQUESTS)
    assert fetched["jira_issues"].num_rows < len(JIRA_ISSUES)


# --------------------------------------------------------------------------
# Gate 4 — CLS masking (HLD §9: mask kind is `hash`, so values are MD5 digests)
# --------------------------------------------------------------------------


def test_cls_mask_hashes_reporter_email():
    table = run_for(CLS_DEMO_SQL, "alice", mask=True)
    emails = table.column("reporter_email").to_pylist()

    assert "reporter_email" in table.column_names, "alias_ must keep the output name"
    assert emails, "the CLS demo preset should return rows"
    assert not any("@" in e for e in emails), "a raw email leaked through the mask"
    assert all(len(e) == 32 for e in emails), "mask kind is `hash` -> MD5 digest"


# --------------------------------------------------------------------------
# Gate 5 — total ordering (HLD §9: the cursor is an offset)
# --------------------------------------------------------------------------


def test_order_by_tiebreaker_gives_a_total_order():
    """`issue.updated DESC, issue.key ASC`. The tiebreaker is not cosmetic: an
    offset cursor over a non-total order skips or duplicates rows."""
    assert run_for(CANONICAL_SQL, "alice").column("key").to_pylist() == [
        "SUP-11",
        "SUP-12",
        "SUP-13",
    ]
