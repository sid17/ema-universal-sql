"""**GATE: `test_entitlement_denied`** + `test_default_deny_is_empty`.

The two halves of ADR-031's split, asserted at the route. They exist as separate
tests because they are separate *outcomes*, and conflating them is exactly the
bug the phase file originally carried:

- an explicit ``effect='deny'`` matching a referenced resource -> **403**. The
  caller asked for something they are forbidden to see, and "zero rows" would be
  a lie;
- no matching ``allow`` (default-deny) -> **empty 200**. The caller has no grant
  for this slice, and an empty answer is the correct answer.
"""


def test_entitlement_denied(run_query):
    """**GATE.** An `auditor` hits the seeded deny on `jira.issues`.

    This is what keeps `ENTITLEMENT_DENIED` reachable. A declared-but-unreachable
    code in a six-code vocabulary the design document publishes is a claim, not
    a feature.
    """
    response = run_query(user="alice", role="auditor")
    assert response.status_code == 403
    body = response.json()
    assert body["error_code"] == "ENTITLEMENT_DENIED"
    assert "jira.issues" in body["message"]
    assert "suggested_action" in body


def test_a_deny_is_not_an_empty_200(run_query):
    """Stated as its own assertion because it is the distinction that matters:
    a denied caller must be able to tell "forbidden" from "nothing matched"."""
    response = run_query(user="alice", role="auditor")
    assert response.status_code != 200


def test_default_deny_is_empty(envelope):
    """**The other half.** `contractor` matches neither the support allow nor
    the auditor deny, so `jira.issues` is governed but ungranted for them."""
    body = envelope(user="dave", role="contractor")
    assert body["rows"] == []
    assert body["partial"] is False
    assert body["join_status"] == "complete"
    assert body["warnings"] == []


def test_a_support_user_is_unaffected_by_the_auditor_deny(envelope):
    """The deny must bind to its role and no other. Fail-closed for everyone
    would be safer than fail-open and still wrong."""
    assert len(envelope(user="alice", role="support")["rows"]) == 3


def test_deny_overrides_a_matching_allow(run_query):
    """Deny-overrides at the route. The engine's unit test covers the decision;
    this covers the rendering of it."""
    assert run_query(user="alice", role="auditor").status_code == 403
    assert run_query(user="alice", role="support").status_code == 200


def test_an_ungoverned_resource_stays_readable(envelope):
    """`github.pull_requests` is named by no policy at all.

    Under a naive "no allow means deny" reading it would yield empty for
    everyone and the headline demo would return zero rows. Access to a resource
    is default-deny at the GRANT (layer L3); policies refine rows and columns
    within an already-granted resource (layer L4).
    """
    body = envelope(
        sql="SELECT pr.title FROM github.pull_requests pr WHERE pr.repo = 'ema/core'",
        user="dave",
        role="contractor",
    )
    assert len(body["rows"]) > 0
