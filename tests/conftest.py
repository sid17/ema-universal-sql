"""Shared fixtures.

Integration tests run against the **running stack** (`make up`), not an in-process
app, because what Phase 0 claims is that the containers come up and serve — an
in-process TestClient would pass even with a broken Dockerfile or a compose file
that never starts Postgres.
"""

from __future__ import annotations

import os

import httpx
import pytest

BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")

_UNREACHABLE = (
    f"The stack is not answering at {BASE_URL}.\n"
    "Integration tests need the containers running: `make up` first, "
    "then `make test-integration`.\n"
    "(`make test` runs the unit suite and needs no Docker.)"
)


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def client(base_url: str):
    """An HTTP client against the running app.

    Fails with an explanation rather than a connection-refused traceback — a
    reviewer who runs the wrong make target should be told which one they wanted.
    """
    with httpx.Client(base_url=base_url, timeout=10.0) as http:
        try:
            http.get("/healthz")
        except httpx.ConnectError:
            pytest.fail(_UNREACHABLE, pytrace=False)
        yield http


@pytest.fixture
def token(client: httpx.Client):
    """Mint a persona token. Defaults to alice on tenant_acme."""

    def _mint(user: str = "alice", role: str = "support", tenant: str = "tenant_acme") -> str:
        response = client.post(
            "/v1/auth/mock-token",
            json={"user": user, "role": role, "tenant": tenant},
        )
        response.raise_for_status()
        return response.json()["token"]

    return _mint


@pytest.fixture
def auth_headers(token):
    def _headers(**kwargs) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(**kwargs)}"}

    return _headers


# --- Phase 2: the query surface ---------------------------------------------

#: The canonical query, verbatim (HLD §4 = design-doc §6.1 — a provenance rail).
CANONICAL_SQL = """
SELECT pr.title, pr.author, issue.key, issue.status
FROM   github.pull_requests pr
JOIN   jira.issues issue ON pr.issue_key = issue.key
WHERE  pr.repo = 'ema/core' AND pr.state = 'open' AND issue.status = 'In Progress'
ORDER BY issue.updated DESC
LIMIT 50
"""

#: The canonical query projects no `reporter_email`, so it cannot itself show
#: CLS. This is the console's second preset (HLD §4).
CLS_DEMO_SQL = """
SELECT pr.title, pr.author, issue.key, issue.status, issue.reporter_email
FROM   github.pull_requests pr
JOIN   jira.issues issue ON pr.issue_key = issue.key
WHERE  pr.repo = 'ema/core' AND pr.state = 'open' AND issue.status = 'In Progress'
ORDER BY issue.updated DESC
LIMIT 50
"""


@pytest.fixture(autouse=True)
def reset_state(client):
    """Flush Redis and drop cached control-plane reads before every test.

    Without this, whether a test sees `served: live` or `served: cache` depends
    on which tests ran before it — so the staleness assertions would pass or
    fail based on pytest's collection order, which is the definition of a flaky
    suite.

    Skipped silently when TEST_MODE is off (the route 404s); the tests that
    genuinely need the hooks say so themselves.
    """
    client.post("/v1/test/reset")
    return None


@pytest.fixture
def run_query(client, auth_headers):
    """POST /v1/query as a persona, returning the raw response."""

    def _run(
        sql: str = CANONICAL_SQL,
        user: str = "alice",
        role: str = "support",
        tenant: str = "tenant_acme",
        max_staleness_ms: int = 0,
        cursor: str | None = None,
    ):
        body: dict = {"sql": sql, "max_staleness_ms": max_staleness_ms}
        if cursor is not None:
            body["cursor"] = cursor
        return client.post(
            "/v1/query",
            headers=auth_headers(user=user, role=role, tenant=tenant),
            json=body,
        )

    return _run


@pytest.fixture
def envelope(run_query):
    """POST /v1/query and assert a 200, returning the envelope."""

    def _envelope(**kwargs):
        response = run_query(**kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    return _envelope


@pytest.fixture
def fail_next(client):
    """Arm a one-shot connector failure, or skip the test if TEST_MODE is off."""

    def _arm(connector: str, mode: str = "timeout"):
        response = client.post(
            "/v1/test/fail-next", json={"connector": connector, "mode": mode}
        )
        if response.status_code == 404:
            pytest.skip(
                "POST /v1/test/fail-next is disabled. Run `make test-integration` "
                "(which sets TEST_MODE=1) rather than pytest directly."
            )
        assert response.status_code == 200, response.text

    return _arm
