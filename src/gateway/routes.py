"""The HTTP surface.

Phase 0 wires every route the prototype will ever expose, but ``/v1/query``
returns an **envelope shell** — parsing, entitlement, planning and execution land
in Phase 2. The point of doing it this way round is that the response contract
exists and is already in use before anything fills it, so no later phase gets to
invent its own shape.

Routes live here rather than in ``main.py`` because handlers are capped at 80
lines and Phases 2, 3 and 4 each rewire one of them; ``main.py`` stays the app
factory. This matches HLD §8's ``src/gateway/`` intent.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from src.config import get_settings
from src.gateway.auth import mint_mock_token
from src.gateway.deps import CurrentUser, Repository
from src.models.envelope import QueryEnvelope
from src.models.request import QueryRequest
from src.observability.metrics import render_metrics
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
        # LAW 4: logged in full with the traceback, never swallowed — but the
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
    """The one Prometheus endpoint (ADR-015): golden signals + our own gauge."""
    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)


@router.post("/v1/auth/mock-token", response_model=MockTokenResponse, tags=["auth"])
def mock_token(body: MockTokenRequest) -> MockTokenResponse:
    """Mint a persona token. Stands in for a per-tenant OIDC provider (HLD §2)."""
    return MockTokenResponse(token=mint_mock_token(body.user, body.role, body.tenant, body.scopes))


@router.post("/v1/query", response_model=QueryEnvelope, tags=["query"])
@stage_span("gateway")
def query(body: QueryRequest, user: CurrentUser, request: Request) -> QueryEnvelope:
    """Run a federated query. **Phase 0 returns the shell** — see the module docstring.

    The deadline attached here bounds the whole pipeline and becomes the parent
    of the per-source budgets in Phases 1-2, so a slow connector degrades the
    response to ``partial`` instead of hanging the request (brief line 84).
    """
    settings = get_settings()
    # NOTE: request.state.user is set by get_current_user (deps.py) — identity
    # is assigned in exactly one place.
    request.state.deadline_ms = settings.REQUEST_TIMEOUT_MS

    return QueryEnvelope(trace_id=current_trace_id())


@router.post("/v1/query/async", tags=["query"], status_code=status.HTTP_501_NOT_IMPLEMENTED)
def query_async() -> dict[str, str]:
    """501, deliberately — and deliberately **not** 404.

    The async reroute is a documented non-goal (HLD §7), but a 429's
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


@router.post("/v1/test/reset", tags=["ops"])
def test_reset(request: Request, repository: Repository) -> dict[str, str]:
    """Flush Redis and drop cached control-plane reads. Test-only.

    Guarded by ``TEST_MODE``: returns 404 otherwise, so the route does not exist
    at all in a normal run rather than existing and refusing. Phase 3's Playwright
    ``beforeEach`` and Phase 4's k6 both need a deterministic starting state —
    without the cache drop, a re-seed would keep serving the old seed for up to
    ``CONTROL_PLANE_TTL_MS``.
    """
    if not get_settings().TEST_MODE:
        # Plain 404, NOT ErrorCode.ENTITLEMENT_DENIED: HLD §9 reserves that code
        # for an explicit policy deny. Labelling a "route disabled" 404 with it
        # would pollute the six-code vocabulary the design doc shares.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is None:
        raise RuntimeError("redis is not configured on app.state")
    redis_client.flushdb()
    repository.invalidate()

    return {"status": "reset"}
