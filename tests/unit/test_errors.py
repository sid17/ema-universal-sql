"""The six-code vocabulary and the one handler that renders it.

The app is built inside the test on purpose: ``src/main.py`` belongs to another
task, and the handler contract must hold for any app it is installed on.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.gateway.handlers import install_error_handlers
from src.models.errors import ApiError, ErrorCode, UnauthenticatedError

EXPECTED_CODES = {
    "RATE_LIMIT_EXHAUSTED",
    "STALE_DATA",
    "ENTITLEMENT_DENIED",
    "SOURCE_TIMEOUT",
    "CONNECTOR_NOT_ENABLED",
    "CONNECTOR_AUTH_ERROR",
}


@pytest.fixture
def client() -> TestClient:
    """A throwaway app whose routes raise the exceptions under test."""
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/rate-limited")
    def rate_limited():
        raise ApiError(
            code=ErrorCode.RATE_LIMIT_EXHAUSTED,
            http=429,
            message="Tenant budget exhausted for github",
            retry_after_ms=1500,
        )

    @app.get("/denied")
    def denied():
        raise ApiError(
            code=ErrorCode.ENTITLEMENT_DENIED,
            http=403,
            message="Explicit deny on jira.issues",
        )

    @app.get("/timeout")
    def timed_out():
        raise ApiError(
            code=ErrorCode.SOURCE_TIMEOUT,
            http=504,
            message="jira exceeded its slice of the request deadline",
            retry_after_ms=1,
            suggested_action="Retry with a larger max_staleness_ms to allow a cached read",
        )

    @app.get("/unauthenticated")
    def unauthenticated():
        raise UnauthenticatedError("Missing or invalid bearer token")

    return TestClient(app, raise_server_exceptions=False)


def test_exactly_the_six_domain_codes_exist():
    assert {code.value for code in ErrorCode} == EXPECTED_CODES
    assert len(ErrorCode) == 6


def test_codes_are_plain_strings():
    assert ErrorCode.STALE_DATA == "STALE_DATA"


def test_unauthenticated_is_not_a_domain_code():
    assert "UNAUTHENTICATED" not in {code.value for code in ErrorCode}
    with pytest.raises(ValueError):
        ErrorCode("UNAUTHENTICATED")


def test_rate_limit_response_sets_retry_after_header_in_whole_seconds(client):
    response = client.get("/rate-limited")
    assert response.status_code == 429
    # 1500 ms rounds up: never advise a retry earlier than the budget refills.
    assert response.headers["Retry-After"] == "2"


def test_rate_limit_body_names_the_async_reroute(client):
    body = client.get("/rate-limited").json()
    assert body["error_code"] == "RATE_LIMIT_EXHAUSTED"
    assert body["retry_after_ms"] == 1500
    assert "/v1/query/async" in body["suggested_action"]


def test_handler_returns_the_declared_http_status(client):
    assert client.get("/denied").status_code == 403
    assert client.get("/timeout").status_code == 504


def test_optional_fields_are_omitted_when_unset(client):
    body = client.get("/denied").json()
    assert body == {
        "error_code": "ENTITLEMENT_DENIED",
        "message": "Explicit deny on jira.issues",
    }
    assert "Retry-After" not in client.get("/denied").headers


def test_explicit_suggested_action_is_not_overwritten(client):
    body = client.get("/timeout").json()
    assert body["suggested_action"] == (
        "Retry with a larger max_staleness_ms to allow a cached read"
    )
    assert client.get("/timeout").headers["Retry-After"] == "1"


def test_unauthenticated_maps_to_401(client):
    response = client.get("/unauthenticated")
    assert response.status_code == 401
    assert response.json()["error_code"] == "UNAUTHENTICATED"


def test_non_rate_limit_error_gets_no_default_suggested_action():
    error = ApiError(code=ErrorCode.STALE_DATA, http=200, message="served from cache")
    assert error.suggested_action is None
