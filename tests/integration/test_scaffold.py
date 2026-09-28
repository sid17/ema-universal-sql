"""Phase 0's gate, against the real stack.

What this proves that the unit suite cannot: the image builds, compose starts
three services in the right order, migrations run against a real Postgres, and
the app serves the typed contract over HTTP. Every assertion here is one of the
phase's "Done when" conditions.

Requires `make up`. Run with `make test-integration`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.models.envelope import QueryEnvelope

CANONICAL_SQL = """
SELECT pr.title, pr.author, issue.key, issue.status
FROM   github.pull_requests pr
JOIN   jira.issues issue ON pr.issue_key = issue.key
WHERE  pr.repo = 'ema/core' AND pr.state = 'open' AND issue.status = 'In Progress'
ORDER BY issue.updated DESC
LIMIT 50
"""


# --------------------------------------------------------------------------
# The stack is up
# --------------------------------------------------------------------------


def test_healthz_is_green(client):
    """Green means *both* Postgres and Redis answered, not just that FastAPI booted."""
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_migrations_created_the_control_plane(client, auth_headers):
    """Assert the schema, not just that the app booted.

    The previous version of this test re-asserted `/healthz == 200`, which
    `test_healthz_is_green` already covers — it would have passed against an
    empty database. The tenant gate is the cheapest real probe of the schema:
    it can only answer 200 if `tenants` exists AND `002_seed_tenants.sql`
    populated it.
    """
    assert (
        client.post("/v1/query", json={"sql": "SELECT 1"}, headers=auth_headers()).status_code
        == 200
    ), "tenants table missing or unseeded"

    assert (
        client.post(
            "/v1/query", json={"sql": "SELECT 1"}, headers=auth_headers(tenant="tenant_offboarded")
        ).status_code
        == 403
    ), "the offboarded seed row is missing, so status is not being read"


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def test_query_without_a_token_is_401(client):
    response = client.post("/v1/query", json={"sql": "SELECT 1"})

    assert response.status_code == 401
    assert response.json()["error_code"] == "UNAUTHENTICATED"


def test_query_with_a_garbage_token_is_401(client):
    response = client.post(
        "/v1/query",
        json={"sql": "SELECT 1"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )

    assert response.status_code == 401


def test_mock_token_mints_a_usable_credential(client):
    response = client.post(
        "/v1/auth/mock-token",
        json={"user": "alice", "role": "support", "tenant": "tenant_acme"},
    )

    assert response.status_code == 200
    assert response.json()["token"].count(".") == 2, "expected a three-part JWT"


# --------------------------------------------------------------------------
# The envelope contract
# --------------------------------------------------------------------------


def test_query_with_a_valid_token_returns_the_envelope_shell(client, auth_headers):
    response = client.post(
        "/v1/query",
        json={"sql": CANONICAL_SQL, "max_staleness_ms": 60000},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()

    # The contract must validate, not merely look similar.
    envelope = QueryEnvelope.model_validate(body)

    assert envelope.rows == [], "Phase 0 returns a shell; execution lands in Phase 2"
    assert envelope.trace_id, "trace_id must be populated on every response"
    assert len(envelope.trace_id) == 32, "a resolvable 32-hex OTel trace id"
    assert envelope.trace_id != "0" * 32, "an all-zero id means no active span"
    assert envelope.partial is False
    assert envelope.join_status == "n/a"
    assert envelope.next_cursor is None


def test_every_response_carries_a_distinct_trace_id(client, auth_headers):
    """A constant trace_id would make the Phase 4 waterfall meaningless."""
    headers = auth_headers()
    first = client.post("/v1/query", json={"sql": "SELECT 1"}, headers=headers).json()
    second = client.post("/v1/query", json={"sql": "SELECT 1"}, headers=headers).json()

    assert first["trace_id"] != second["trace_id"]


def test_envelope_rejects_a_shape_that_is_not_the_contract():
    """Guards the guard: model_validate above must actually be able to fail."""
    with pytest.raises(ValidationError):
        QueryEnvelope.model_validate({"rows": [], "join_status": "sideways", "trace_id": "x"})


# --------------------------------------------------------------------------
# Documented non-goals and ops surface
# --------------------------------------------------------------------------


def test_async_query_is_501_not_404(client):
    """A 429's suggested_action points here. A 404 would read as a bug."""
    response = client.post("/v1/query/async", json={})

    assert response.status_code == 501
    assert "non-goal" in response.json()["message"]


def test_metrics_exposes_both_families(client):
    """ADR-015's load-bearing assumption: golden signals and our own domain
    gauge share one registry, so one scrape carries both."""
    body = client.get("/metrics").text

    assert "rate_limit_remaining" in body, "the per-connector gauge is missing"
    assert "http_request" in body, "the golden-signal collectors are missing"


def test_test_reset_matches_the_configured_test_mode(client):
    """Assert the actual contract, not "one of two acceptable answers".

    The previous version accepted 404 *or* 200 and skipped on 200 — so deleting
    the TEST_MODE guard from the route entirely would have left it passing.
    """
    from src.config import get_settings

    expected = 200 if get_settings().TEST_MODE else 404
    response = client.post("/v1/test/reset", json={})

    assert response.status_code == expected, (
        f"TEST_MODE={get_settings().TEST_MODE} should give {expected}, got {response.status_code}"
    )


# --------------------------------------------------------------------------
# The tenant-status gate, against real seeded rows
# --------------------------------------------------------------------------


def test_offboarded_tenant_is_refused_with_403(client, auth_headers):
    """Crypto-shred's front half, proven against the control plane rather than a stub.

    The token is perfectly valid — this is authorisation refusing after
    authentication succeeded, which is why it is 403 ENTITLEMENT_DENIED and not 401.
    """
    response = client.post(
        "/v1/query",
        json={"sql": "SELECT 1"},
        headers=auth_headers(user="alice", tenant="tenant_offboarded"),
    )

    assert response.status_code == 403
    body = response.json()
    assert body["error_code"] == "ENTITLEMENT_DENIED"
    # The body must NOT reveal WHY. An unknown tenant and an offboarded one give
    # byte-identical responses, or a caller can enumerate which tenants exist and
    # which are suspended — and the mock IdP lets anyone name any tenant.
    assert "offboarding" not in body["message"]
    assert "tenant_offboarded" not in body["message"]

    unknown = client.post(
        "/v1/query",
        json={"sql": "SELECT 1"},
        headers=auth_headers(tenant="tenant_does_not_exist"),
    )
    assert unknown.json() == body, "unknown vs offboarded must be indistinguishable"


def test_unknown_tenant_is_refused_with_403(client, auth_headers):
    """A validly-signed token may name any tenant; unknown must not mean allowed."""
    response = client.post(
        "/v1/query",
        json={"sql": "SELECT 1"},
        headers=auth_headers(tenant="tenant_does_not_exist"),
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == "ENTITLEMENT_DENIED"


def test_active_tenants_all_pass_the_gate(client, auth_headers):
    """tenant_load in particular: k6 targets it in Phase 4 and must not 403."""
    for tenant in ("tenant_acme", "tenant_globex", "tenant_load"):
        response = client.post(
            "/v1/query", json={"sql": "SELECT 1"}, headers=auth_headers(tenant=tenant)
        )
        assert response.status_code == 200, f"{tenant} was refused"


# --------------------------------------------------------------------------
# L2 — the coarse scope gate, end to end
# --------------------------------------------------------------------------


def test_a_token_without_query_execute_is_refused(client):
    """The brief's line 157 — "user token -> scopes/roles -> RLS/CLS" — starts here.

    Coarse, and deliberately so: this refuses before anything is planned or
    fetched. Row- and column-level decisions are compiled into the plan in
    Phase 2, never made at the endpoint.
    """
    underprivileged = client.post(
        "/v1/auth/mock-token",
        json={"user": "alice", "role": "support", "tenant": "tenant_acme", "scopes": ""},
    ).json()["token"]

    response = client.post(
        "/v1/query",
        json={"sql": "SELECT 1"},
        headers={"Authorization": f"Bearer {underprivileged}"},
    )

    assert response.status_code == 403
    body = response.json()
    assert body["error_code"] == "ENTITLEMENT_DENIED"
    assert "query:execute" in body["message"]


def test_an_unrelated_scope_does_not_open_the_gate(client):
    token = client.post(
        "/v1/auth/mock-token",
        json={
            "user": "alice",
            "role": "support",
            "tenant": "tenant_acme",
            "scopes": "profile connector:github",
        },
    ).json()["token"]

    response = client.post(
        "/v1/query", json={"sql": "SELECT 1"}, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403


def test_the_default_token_carries_the_scope_and_passes(client, auth_headers):
    assert (
        client.post("/v1/query", json={"sql": "SELECT 1"}, headers=auth_headers()).status_code
        == 200
    )
