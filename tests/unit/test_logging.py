"""The access log, and the claim that makes it worth having.

A ``trace_id`` is uninterpretable on its own — the trace says where the time
went, not what was asked or by whom. So the load-bearing assertion here is
**correlation**: the id in the log line is the same id the response carried and
the span recorded. If that ever drifts, both artifacts become decorative.

Infra-free: a throwaway app, no Postgres, no Redis.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from src.models.context import UserContext
from src.observability.logging import (
    ACCESS_LOGGER,
    JsonFormatter,
    RequestLogMiddleware,
)
from src.observability.tracing import configure_tracing, current_trace_id


@pytest.fixture
def access_lines(caplog):
    """Capture access-log records as parsed JSON."""
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    formatter = JsonFormatter()

    def _lines() -> list[dict]:
        return [
            json.loads(formatter.format(record))
            for record in caplog.records
            if record.name == ACCESS_LOGGER
        ]

    return _lines


def build_app(*, authenticated: bool = True) -> FastAPI:
    """A minimal app with the middleware, instrumented the way main.py does."""
    # main.py calls this in create_app(); without a provider there is no ambient
    # span and current_trace_id() is empty by design.
    configure_tracing()

    app = FastAPI()
    app.add_middleware(RequestLogMiddleware)

    @app.get("/v1/thing")
    def thing(request: Request):
        if authenticated:
            request.state.user = UserContext(
                tenant_id="tenant_acme",
                user_id="alice",
                roles=("support",),
                scopes=frozenset({"query:execute"}),
                token_id="jti-123",
            )
        return {"trace_id": current_trace_id()}

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    FastAPIInstrumentor.instrument_app(app)
    return app


# --------------------------------------------------------------------------
# The formatter
# --------------------------------------------------------------------------


def test_formatter_emits_one_line_of_valid_json():
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=0, msg="hello", args=(), exc_info=None
    )
    record.context = {"trace_id": "abc", "status": 200}

    rendered = JsonFormatter().format(record)

    assert "\n" not in rendered, "a multi-line log line is not greppable"
    payload = json.loads(rendered)
    assert payload["message"] == "hello"
    assert payload["trace_id"] == "abc"
    assert payload["status"] == 200


# --------------------------------------------------------------------------
# The access line
# --------------------------------------------------------------------------


def test_one_line_per_request_with_identity(access_lines):
    client = TestClient(build_app())

    client.get("/v1/thing")

    lines = access_lines()
    assert len(lines) == 1
    line = lines[0]
    assert line["method"] == "GET"
    assert line["path"] == "/v1/thing"
    assert line["status"] == 200
    assert line["tenant_id"] == "tenant_acme"
    assert line["user_id"] == "alice"
    assert line["token_id"] == "jti-123", "jti names the credential without logging it"
    assert line["duration_ms"] >= 0


def test_trace_id_in_the_log_matches_the_response(access_lines):
    """The whole point. Same id in the response body and the access line.

    This also proves the middleware sits INSIDE the instrumentor's server span —
    if it were outside, there would be no ambient span and trace_id would be null.
    """
    client = TestClient(build_app())

    response = client.get("/v1/thing")
    body_trace_id = response.json()["trace_id"]

    logged = access_lines()[0]["trace_id"]
    assert logged, "no ambient trace — the middleware is outside the server span"
    assert logged == body_trace_id
    assert len(logged) == 32


def test_unauthenticated_request_logs_null_identity(access_lines):
    """Worth recording as null rather than claiming a caller we never resolved."""
    client = TestClient(build_app(authenticated=False))

    client.get("/v1/thing")

    line = access_lines()[0]
    assert line["tenant_id"] is None
    assert line["user_id"] is None
    assert line["token_id"] is None


def test_polled_ops_routes_are_not_logged(access_lines):
    """/healthz is polled by compose every few seconds; it would drown the log."""
    client = TestClient(build_app())

    client.get("/healthz")

    assert access_lines() == []


def test_the_token_itself_is_never_logged(access_lines):
    """A credential in the access log is a credential in everyone's log pipeline."""
    client = TestClient(build_app())

    client.get("/v1/thing", headers={"Authorization": "Bearer super-secret-token-value"})

    rendered = json.dumps(access_lines())
    assert "super-secret-token-value" not in rendered
    assert "authorization" not in rendered.lower()
