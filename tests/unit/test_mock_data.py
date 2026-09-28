"""The persona contract — asserted against the raw constants, before any adapter.

If these counts drift, the RLS demo silently stops being 3 -> 1 and nobody finds
out until Phase 2's gate. Asserting here means a data edit fails immediately,
with a message about the data, rather than surfacing later as a confusing
entitlement bug.
"""

import pytest

from src.connectors.mock_data import (
    GITHUB_COLUMNS,
    GITHUB_PULL_REQUESTS,
    JIRA_COLUMNS,
    JIRA_ISSUES,
    github_rows,
    jira_rows,
)

ISSUES_BY_KEY = {issue["key"]: issue for issue in JIRA_ISSUES}


def canonical_join(assignee: str | None = None) -> list[dict]:
    """The canonical query, in Python, with the RLS predicate optional.

    Open PRs in ``ema/core`` joined to ``In Progress`` issues — HLD §4. Written
    out rather than imported so this test does not depend on the pipeline it is
    meant to constrain; Phase 2 must reproduce these numbers independently.
    """
    joined = []
    for pr in GITHUB_PULL_REQUESTS:
        if pr["repo"] != "ema/core" or pr["state"] != "open":
            continue
        issue = ISSUES_BY_KEY.get(pr["issue_key"])
        if issue is None or issue["status"] != "In Progress":
            continue
        if assignee is not None and issue["assignee"] != assignee:
            continue
        joined.append({"pr": pr, "issue": issue})
    return joined


# --- the contract ----------------------------------------------------------


def test_unfiltered_baseline_is_eight_rows():
    """Bigger than any single persona's slice, so RLS visibly removes real rows."""
    assert len(canonical_join()) == 8


@pytest.mark.parametrize("persona,expected", [("alice", 3), ("bob", 1), ("carol", 0)])
def test_persona_row_counts(persona, expected):
    assert len(canonical_join(persona)) == expected


def test_rls_shrinks_rather_than_empties():
    """bob must be 1, not 0.

    A count that drops to zero is indistinguishable from a broken query, so the
    demo would prove nothing. This is the assertion that keeps it meaningful.
    """
    alice, bob = len(canonical_join("alice")), len(canonical_join("bob"))
    assert alice > bob > 0


def test_carol_is_empty_through_the_join_not_through_absence():
    """carol has In Progress work; her only PR is closed.

    That makes her the `empty` leg of the trichotomy for an interesting reason —
    the join found nothing — rather than because she has no data at all.
    """
    carol_in_progress = [
        i for i in JIRA_ISSUES if i["assignee"] == "carol" and i["status"] == "In Progress"
    ]
    assert carol_in_progress, "carol must own In Progress work"
    keys = {i["key"] for i in carol_in_progress}
    prs = [p for p in GITHUB_PULL_REQUESTS if p["issue_key"] in keys]
    assert prs, "carol's issue must have a PR"
    assert all(p["state"] == "closed" for p in prs)
    assert canonical_join("carol") == []


# --- the negative rows, each asserted to actually bite ---------------------


def test_a_closed_pr_exists_for_an_alice_issue():
    """Without it, the `state` predicate is vacuously true for alice.

    PR#110 is closed and points at SUP-12. If `state` silently stopped applying,
    alice's count would become 4 and `test_persona_row_counts` would catch it.
    """
    closed = [
        p for p in GITHUB_PULL_REQUESTS
        if p["issue_key"] == "SUP-12" and p["state"] == "closed"
    ]
    assert closed, "SUP-12 needs a closed PR or the state filter proves nothing"


def test_an_open_pr_in_another_repo_exists_for_an_alice_issue():
    """Same argument for the `repo` predicate. PR#111 is open, but in ema/docs."""
    other_repo = [
        p for p in GITHUB_PULL_REQUESTS
        if p["issue_key"] == "SUP-13" and p["state"] == "open" and p["repo"] != "ema/core"
    ]
    assert other_repo, "SUP-13 needs an open PR outside ema/core"


def test_dropping_the_state_filter_would_change_alices_count():
    """Proves the negatives bite, rather than merely existing."""
    without_state = [
        pr for pr in GITHUB_PULL_REQUESTS
        if pr["repo"] == "ema/core"
        and ISSUES_BY_KEY.get(pr["issue_key"], {}).get("status") == "In Progress"
        and ISSUES_BY_KEY.get(pr["issue_key"], {}).get("assignee") == "alice"
    ]
    assert len(without_state) > len(canonical_join("alice"))


def test_dropping_the_repo_filter_would_change_alices_count():
    without_repo = [
        pr for pr in GITHUB_PULL_REQUESTS
        if pr["state"] == "open"
        and ISSUES_BY_KEY.get(pr["issue_key"], {}).get("status") == "In Progress"
        and ISSUES_BY_KEY.get(pr["issue_key"], {}).get("assignee") == "alice"
    ]
    assert len(without_repo) > len(canonical_join("alice"))


def test_an_unjoinable_pr_exists():
    """Phase 2's join_status: incomplete leg needs a PR with no matching issue."""
    orphans = [
        p for p in GITHUB_PULL_REQUESTS
        if p["repo"] == "ema/core" and p["state"] == "open" and p["issue_key"] not in ISSUES_BY_KEY
    ]
    assert orphans, "an open ema/core PR with no matching issue is required"


def test_every_status_is_represented():
    assert {i["status"] for i in JIRA_ISSUES} == {"In Progress", "Done", "To Do"}


# --- data hygiene ----------------------------------------------------------


def test_reporter_emails_are_all_distinct():
    """The CLS-masked column.

    Duplicates would let a masked value be re-identified by matching it against
    an unmasked row elsewhere in the same result.
    """
    emails = [i["reporter_email"] for i in JIRA_ISSUES]
    assert len(emails) == len(set(emails))


def test_issue_keys_are_unique():
    keys = [i["key"] for i in JIRA_ISSUES]
    assert len(keys) == len(set(keys))


def test_pr_numbers_are_unique():
    numbers = [p["number"] for p in GITHUB_PULL_REQUESTS]
    assert len(numbers) == len(set(numbers))


def test_datasets_are_the_advertised_size():
    assert len(JIRA_ISSUES) == 20
    assert len(GITHUB_PULL_REQUESTS) == 20


def test_every_row_has_exactly_the_declared_columns():
    """The capability model promises these columns; a typo would surface here."""
    for issue in JIRA_ISSUES:
        assert tuple(issue) == JIRA_COLUMNS
    for pr in GITHUB_PULL_REQUESTS:
        assert tuple(pr) == GITHUB_COLUMNS


def test_no_row_contains_a_generated_value():
    """Determinism: every timestamp is a fixed literal, all in 2026-09."""
    stamps = [i["updated"] for i in JIRA_ISSUES]
    stamps += [p["created_at"] for p in GITHUB_PULL_REQUESTS]
    stamps += [p["updated_at"] for p in GITHUB_PULL_REQUESTS]
    assert all(s.startswith("2026-09-") and s.endswith("Z") for s in stamps)


# --- defensive copies ------------------------------------------------------


def test_accessors_return_copies_not_the_constants():
    """An adapter filtering in place would corrupt every later request."""
    rows = github_rows()
    assert rows == GITHUB_PULL_REQUESTS
    assert rows is not GITHUB_PULL_REQUESTS

    rows[0]["title"] = "MUTATED"
    rows.pop()
    assert GITHUB_PULL_REQUESTS[0]["title"] != "MUTATED"
    assert len(GITHUB_PULL_REQUESTS) == 20


def test_jira_accessor_returns_copies_too():
    rows = jira_rows()
    rows[0]["assignee"] = "mallory"
    assert JIRA_ISSUES[0]["assignee"] == "alice"


def test_repeated_calls_are_equal_but_independent():
    assert github_rows() == github_rows()
    assert github_rows()[0] is not github_rows()[0]
