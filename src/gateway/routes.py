"""The HTTP surface.

Routes live here rather than in ``main.py`` so handlers stay small and
``main.py`` stays the app factory.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from src.config import get_settings
from src.connectors.errors import FailureMode
from src.gateway.auth import mint_mock_token
from src.gateway.deps import CurrentUser, Repository
from src.models.envelope import QueryEnvelope
from src.models.errors import InvalidQueryError
from src.models.request import QueryRequest
from src.observability.metrics import observe_query, render_metrics
from src.observability.tracing import current_trace_id, stage_span

logger = logging.getLogger(__name__)

router = APIRouter()


class MockTokenRequest(BaseModel):
    """A persona to mint a demo credential for."""

    user: str = Field(description="Becomes the JWT 'sub' and the RLS subject.")
    role: str = Field(default="support", description="Becomes the single entry in 'roles'.")
    tenant: str = Field(default="tenant_acme", description="Becomes 'tenant_id'.")
    scopes: str | None = Field(
        default=None,
        description=(
            "Space-delimited OAuth scopes (RFC 8693 §4.2). Defaults to "
            "DEFAULT_SCOPES. Pass an empty string to mint a deliberately "
            "under-privileged token and exercise the gateway's scope gate."
        ),
    )


class MockTokenResponse(BaseModel):
    token: str


class FailNextRequest(BaseModel):
    """Arm a one-shot connector failure. Test-only — see ``fail_next``."""

    connector: str = Field(description="Connector type, e.g. 'jira'.")
    mode: str = Field(
        default="timeout",
        description="One of: timeout, throttled, auth, not_enabled.",
    )


def _require_test_mode() -> None:
    """404 unless ``TEST_MODE`` is on, so the route does not exist in a normal run.

    Plain 404, NOT ``ErrorCode.ENTITLEMENT_DENIED``: that code means an explicit
    policy deny, and labelling a "route disabled" 404 with it would give a
    security code two unrelated meanings. 404 rather than 403 for the same
    reason a disabled route should not advertise itself.
    """
    if not get_settings().TEST_MODE:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


@router.get("/healthz", tags=["ops"])
def healthz(request: Request) -> dict[str, str]:
    """Ready only when **both** backing stores answer.

    Reporting ok while Postgres is unreachable would let compose declare the app
    healthy and let `make up` return before the stack can actually serve.
    """
    failures: list[str] = []

    pool = getattr(request.app.state, "pool", None)
    try:
        if pool is None:
            raise RuntimeError("pool not configured")
        with pool.connection() as conn:
            conn.execute("SELECT 1")
    except Exception:
        # Logged in full with the traceback, never swallowed — but the
        # detail stays server-side. /healthz is unauthenticated, and a psycopg
        # or redis error string can carry the DSN, host and credentials.
        logger.exception("healthz: postgres unreachable")
        failures.append("postgres")

    redis_client = getattr(request.app.state, "redis", None)
    try:
        if redis_client is None:
            raise RuntimeError("redis not configured")
        redis_client.ping()
    except Exception:
        logger.exception("healthz: redis unreachable")
        failures.append("redis")

    if failures:
        # Not one of the six domain codes: those describe query execution, and
        # a dead backing store is an ops condition, not an entitlement or
        # connector outcome.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "unavailable", "unavailable": sorted(failures)},
        )

    return {"status": "ok"}


@router.get("/metrics", tags=["ops"])
def metrics() -> Response:
    """The one Prometheus endpoint: golden signals plus our own gauge."""
    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)


@router.post("/v1/auth/mock-token", response_model=MockTokenResponse, tags=["auth"])
def mock_token(body: MockTokenRequest) -> MockTokenResponse:
    """Mint a persona token. Stands in for a per-tenant OIDC provider."""
    return MockTokenResponse(token=mint_mock_token(body.user, body.role, body.tenant, body.scopes))


@router.post("/v1/query", response_model=QueryEnvelope, tags=["query"])
@stage_span("gateway")
async def query(body: QueryRequest, user: CurrentUser, request: Request) -> QueryEnvelope:
    """Run a federated query through the five-stage pipeline.

    The handler resolves, calls and returns — deliberately. Every decision worth
    making lives in :class:`~src.pipeline.runner.QueryPipelineRunner`, so the
    stage order is stated once and cannot drift between this route, the tests
    and ``make demo``. (It also keeps the handler well inside the 80-line cap.)

    The deadline attached here bounds the whole pipeline and is the parent of
    the per-source budgets the federation engine enforces, so a slow connector
    degrades the response to ``partial`` instead of hanging the request
    rather than hanging the request.
    """
    settings = get_settings()
    # NOTE: request.state.user is set by get_current_user (deps.py) — identity
    # is assigned in exactly one place.
    request.state.deadline_ms = settings.REQUEST_TIMEOUT_MS

    runner = getattr(request.app.state, "runner", None)
    if runner is None:
        # A wiring bug, not a caller problem. Returning an empty envelope would
        # be indistinguishable from a query that legitimately matched nothing —
        # the `empty` leg of the trichotomy, claimed falsely.
        raise RuntimeError("query pipeline is not configured on app.state")

    # Timed HERE, not inside the runner, and in a `finally`. A
    # malformed query and an entitlement denial both raise before the pipeline
    # produces an envelope, so a histogram fed from the runner would see only
    # the requests that succeeded and would report a P95 better than the one
    # callers actually experience — which is the exact failure mode a latency
    # metric exists to prevent.
    started = time.perf_counter()
    try:
        return await runner.run(body, user, current_trace_id())
    finally:
        observe_query(time.perf_counter() - started)


@router.post("/v1/query/async", tags=["query"], status_code=status.HTTP_501_NOT_IMPLEMENTED)
def query_async() -> dict[str, str]:
    """501, deliberately — and deliberately **not** 404.

    The async reroute is a documented non-goal, but a 429's
    ``suggested_action`` points callers here. A pointer to a 404 reads as a bug;
    a 501 reads as a scoped decision.
    """
    return {
        "error_code": "NOT_IMPLEMENTED",
        "message": (
            "Async query execution is a documented non-goal of this prototype. "
            "The design covers it as the 202 + job_id reroute; see README."
        ),
    }


@router.post("/v1/test/fail-next", tags=["ops"])
async def test_fail_next(body: FailNextRequest, request: Request) -> dict[str, str]:
    """Make the next fetch of one connector fail. Test-only; 404 unless ``TEST_MODE``.

    This is what makes "a source times out, the answer degrades to partial"
    demonstrable on demand rather than only during a real
    outage. `make demo` uses it for its fourth call, and
    ``tests/integration/test_timeout_partial.py`` for the regression gate.

    One-shot: the request after this one behaves normally, so a demo can show
    the recovery as well as the failure.
    """
    _require_test_mode()

    try:
        mode = FailureMode(body.mode)
    except ValueError as exc:
        raise InvalidQueryError(
            f"unknown failure mode {body.mode!r}; "
            f"expected one of {', '.join(m.value for m in FailureMode)}",
            detail="FailureMode",
        ) from exc

    runner = getattr(request.app.state, "runner", None)
    if runner is None:
        raise RuntimeError("query pipeline is not configured on app.state")
    await runner.registry.fail_next(body.connector, mode)

    return {"status": "armed", "connector": body.connector, "mode": mode.value}


@router.post("/v1/test/reset", tags=["ops"])
def test_reset(request: Request, repository: Repository) -> dict[str, str]:
    """Flush Redis and drop cached control-plane reads. Test-only.

    **Partial by construction, and named rather than hidden.** ``flushdb`` is
    global — every worker sees it, because Redis is shared. ``invalidate()`` is
    not: the control-plane TTL cache lives in this process, so only the worker
    that happened to serve *this* request drops its entries. The other seven
    keep theirs for up to ``CONTROL_PLANE_TTL_MS``.

    That is the same shape as two defects already fixed in this repo — the
    forced-failure hook and the ``rate_limit_remaining`` gauge — per-worker
    state reached through an endpoint that looks global. It has not bitten
    anything: the reads it caches (capabilities, grants, policies, budgets) only
    change on a re-seed, and the tests that re-seed read Postgres directly
    rather than through the app. A test that re-seeds and then queries through
    ``POST /v1/query`` WOULD see stale data from seven workers out of eight.

    Guarded by ``TEST_MODE``: returns 404 otherwise, so the route does not exist
    at all in a normal run rather than existing and refusing. Browser specs and
    the k6 load profile both need a deterministic starting state —
    without the cache drop, a re-seed would keep serving the old seed for up to
    ``CONTROL_PLANE_TTL_MS``.
    """
    _require_test_mode()

    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is None:
        raise RuntimeError("redis is not configured on app.state")
    redis_client.flushdb()
    repository.invalidate()

    return {"status": "reset"}
