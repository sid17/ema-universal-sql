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
