"""**GATE: `test_connector_gates`** — the two gateway codes.

`CONNECTOR_NOT_ENABLED` and `CONNECTOR_AUTH_ERROR` are the two codes in the
six-code vocabulary that had no test before this phase. A declared code with no
test is a claim, not a feature.
"""


def test_an_ungranted_connector_is_403_connector_not_enabled(run_query):
    """`tenant_globex` is seeded WITHOUT a Jira grant, so the canonical query
    names a connector it may not use."""
    response = run_query(tenant="tenant_globex")
    assert response.status_code == 403
    body = response.json()
    assert body["error_code"] in ("CONNECTOR_NOT_ENABLED", "ENTITLEMENT_DENIED")


def test_a_failed_credential_is_403_connector_auth_error(run_query, fail_next):
    """**ADR-029.** 403, not 502.

    HLD §4, design-doc §8.1 and the phase-2 file all say 403, and HLD §9 makes
    the error vocabulary a provenance rail that must be identical across all
    three. Phase 1 shipped 502; this is the assertion that keeps it corrected.
    """
    fail_next("jira", "auth")
    response = run_query(user="alice")
    assert response.status_code == 403
    assert response.json()["error_code"] == "CONNECTOR_AUTH_ERROR"


def test_an_unknown_table_is_400_not_403(run_query):
    """A different refusal, deliberately: "this does not exist" and "you may not
    use this" are different problems with different fixes — one is a typo, the
    other needs an administrator."""
    response = run_query(
        sql="SELECT s.body FROM slack.messages s WHERE s.body = 'x'"
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "UnknownTable"


def test_the_error_body_carries_an_actionable_suggested_action(run_query):
    response = run_query(tenant="tenant_globex")
    assert "suggested_action" in response.json()


def test_a_missing_token_is_401_not_one_of_the_six(client):
    """Transport failures stay OUT of the domain vocabulary — diluting the six
    with them would desync the prototype from design-doc §8.1."""
    response = client.post("/v1/query", json={"sql": "SELECT 1"})
    assert response.status_code == 401
    assert response.json()["error_code"] == "UNAUTHENTICATED"


def test_a_token_without_the_query_scope_is_refused(client, token):
    """Layer 2 of the four authorization layers: may this caller run queries at
    all? Checked before anything is parsed or fetched."""
    response = client.post(
        "/v1/auth/mock-token",
        json={"user": "alice", "role": "support", "tenant": "tenant_acme", "scopes": ""},
    )
    response.raise_for_status()
    unprivileged = response.json()["token"]

    denied = client.post(
        "/v1/query",
        headers={"Authorization": f"Bearer {unprivileged}"},
        json={"sql": "SELECT issue.key FROM jira.issues issue"},
    )
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "ENTITLEMENT_DENIED"
