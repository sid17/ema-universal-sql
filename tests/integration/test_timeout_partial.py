"""**GATE: `test_timeout_partial` + `test_cursor_null_when_partial`.**

DoD §2 hard part 5 — connector reliability and honest degradation. The sharpest
requirement in the phase file is the negative one: the surviving side's rows are
never passed off as the joined answer.
"""


def test_timeout_partial(envelope, fail_next):
    """**GATE.** Jira times out; the answer degrades rather than failing."""
    fail_next("jira", "timeout")
    body = envelope(user="alice")

    assert body["partial"] is True
    assert body["join_status"] == "incomplete"
    assert body["warnings"][0]["code"] == "SOURCE_TIMEOUT"
    assert body["warnings"][0]["connector"] == "jira"

    # GitHub's fetch still happened — the sibling was not cancelled.
    outcomes = {s["connector"]: s for s in body["sources"]}
    assert outcomes["github"]["state"] == "ok"
    assert body["stats"]["connector_ms"].get("github") is not None


def test_the_unjoined_side_is_never_passed_off_as_joined(envelope, fail_next):
    """**The honesty requirement.**

    GitHub returns 14 matching PRs. If those were handed back as "the joined
    answer" a caller would receive pull requests with no issue attached and no
    way to tell. The join produced nothing, so the answer has no rows — and it
    says `incomplete` rather than pretending to be `complete` and empty.
    """
    fail_next("jira", "timeout")
    body = envelope(user="alice")
    assert len(body["rows"]) < 14
    assert body["join_status"] == "incomplete"


def test_a_partial_is_distinguishable_from_an_empty_result(envelope, fail_next):
    """Both can have zero rows. `partial` and `join_status` are the difference,
    which is exactly why the row count must never be the discriminator."""
    fail_next("jira", "timeout")
    partial = envelope(user="alice")
    empty = envelope(user="carol")

    assert partial["rows"] == empty["rows"] == []
    assert partial["partial"] is True
    assert empty["partial"] is False
    assert partial["join_status"] == "incomplete"
    assert empty["join_status"] == "complete"


def test_cursor_null_when_partial(envelope, fail_next):
    """**GATE.** An offset into an incomplete result set is not stable.

    Paging from a partial page would silently skip the rows the failed source
    never contributed — making the omission invisible exactly when it matters
    most. `QueryEnvelope` enforces this structurally too, so neither layer alone
    is load-bearing.
    """
    fail_next("jira", "timeout")
    body = envelope(user="alice")
    assert body["partial"] is True
    assert body["next_cursor"] is None


def test_the_failure_hook_is_one_shot(envelope, fail_next):
    """The request after a forced failure behaves normally, so the demo can show
    recovery as well as failure — and so one armed hook cannot poison the rest
    of the suite."""
    fail_next("jira", "timeout")
    assert envelope(user="alice")["partial"] is True
    assert envelope(user="alice")["partial"] is False


def test_an_auth_failure_is_a_hard_stop_not_a_partial(run_query, fail_next):
    """A permanently broken connector must not be presented as a temporary gap.

    `CONNECTOR_AUTH_ERROR` is 403 (ADR-029) — an administrative problem
    ("reconnect Jira"), not something to retry.
    """
    fail_next("jira", "auth")
    response = run_query(user="alice")
    assert response.status_code == 403
    assert response.json()["error_code"] == "CONNECTOR_AUTH_ERROR"
