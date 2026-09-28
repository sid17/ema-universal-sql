"""**GATE: `test_trichotomy`** — empty ≠ partial ≠ error.

`02-DEFINITION-OF-DONE.md` §4 non-negotiable #3, and the correctness point the
DoD calls "graded but easy to lose". Three outcomes, three distinguishable
envelope shapes, asserted together in one file so the differences are visible
side by side rather than spread across three modules.
"""

import pytest

from tests.conftest import CANONICAL_SQL


def test_empty_is_a_successful_answer(envelope):
    """**EMPTY.** carol ran fine; the filters and her entitlement left no rows.

    She HAS an In-Progress issue (SUP-31) — one row comes back from Jira. Its
    only pull request is closed, so the join produces nothing. An empty result
    that came from an empty fetch would prove much less.
    """
    body = envelope(user="carol")
    assert body["rows"] == []
    assert body["partial"] is False
    assert body["join_status"] == "complete"
    assert body["warnings"] == []
    assert body["next_cursor"] is None
    assert body["freshness_ms"] is not None  # data WAS fetched; it just did not join


def test_partial_is_a_degraded_answer(envelope, fail_next):
    """**PARTIAL.** A source failed, and the envelope says so in four places."""
    fail_next("jira", "timeout")
    body = envelope(user="alice")

    assert body["partial"] is True
    assert body["join_status"] == "incomplete"
    assert [w["code"] for w in body["warnings"]] == ["SOURCE_TIMEOUT"]
    assert body["warnings"][0]["connector"] == "jira"

    outcomes = {s["connector"]: s for s in body["sources"]}
    assert outcomes["jira"]["state"] == "timeout"
    assert outcomes["jira"]["served"] == "none"
    assert outcomes["github"]["state"] == "ok"


def test_error_is_no_answer_at_all(run_query):
    """**ERROR.** Malformed SQL is a 400 with no envelope rows."""
    response = run_query(sql="SELECT FROM WHERE")
    assert response.status_code == 400
    body = response.json()
    assert body["error_code"] == "INVALID_QUERY"
    assert "rows" not in body


def test_the_three_shapes_are_mutually_distinguishable(envelope, run_query, fail_next):
    """The property stated directly: a caller can branch on these without prose.

    This is the test that would fail if, say, an error started returning an
    empty 200 or a partial stopped setting its flag — each of which is a change
    that looks harmless in isolation.
    """
    empty = envelope(user="carol")

    fail_next("jira", "timeout")
    partial = envelope(user="alice")

    error = run_query(sql="SELECT FROM WHERE")

    # Empty and partial are both 200 but differ on `partial`.
    assert empty["partial"] is False and partial["partial"] is True
    # Empty and partial both have rows-or-not; the discriminator is never the
    # row count, because a partial CAN be empty.
    assert empty["join_status"] != partial["join_status"]
    # Error is not a 200 at all and carries no envelope.
    assert error.status_code == 400
    assert "partial" not in error.json()


@pytest.mark.parametrize(
    ("sql", "detail"),
    [
        ("SELECT * FROM jira.issues issue", "Star"),
        ("SELECT COUNT(pr.title) FROM github.pull_requests pr", "Count"),
        ("DROP TABLE jira.issues", "ParseError"),
        ("SELECT issue.nope FROM jira.issues issue", "UnknownColumn"),
    ],
)
def test_every_unsupported_query_is_a_400_naming_the_problem(run_query, sql, detail):
    """"Unsupported SQL" with no pointer is the least actionable error a query
    API can return."""
    response = run_query(sql=sql)
    assert response.status_code == 400
    assert response.json()["detail"] == detail


def test_a_write_is_refused(run_query):
    """The engine is read-only, and `into=exp.Select` enforces it before any
    walk of the tree."""
    assert run_query(sql="DELETE FROM jira.issues").status_code == 400


def test_the_canonical_query_is_still_accepted(run_query):
    """The counterweight to every rejection above: the subset must not have
    been narrowed so far that the query we ship no longer runs."""
    assert run_query(sql=CANONICAL_SQL).status_code == 200
