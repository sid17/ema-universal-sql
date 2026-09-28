"""FastAPI application factory.

Owns wiring only — the routes live in ``src/gateway/routes.py``. The clients
opened here are hung on ``app.state`` so the governance modules (token bucket,
freshness cache, secrets) borrow *these* connections rather than opening
their own; a second pool would double the connection count and make the
control-plane cache's query-count guarantee meaningless.

The lifespan runs migrations at startup. That is deliberate for a prototype whose
submission gate is `make up` on a fresh clone: a separate migrate step is one
more thing a reviewer can forget, and the runner is idempotent (see
``control_plane/db.py``).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis as redis_lib
import redis.asyncio as redis_async
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from src.config import get_settings
from src.control_plane.db import create_pool, run_migrations
from src.control_plane.repository import ControlPlaneRepository
from src.execution.assemble import ResultAssembler
from src.execution.duckdb_pool import DuckDBPool
from src.gateway.handlers import install_error_handlers
from src.gateway.routes import router
from src.governance.audit import AuditLogger
from src.governance.cache import FreshnessCacheManager
from src.governance.failure_hooks import FailureHookStore
from src.governance.ratelimit import TokenBucketRateLimiter
from src.governance.secrets import SecretsManagerClient
from src.observability.logging import RequestLogMiddleware, configure_logging
from src.observability.metrics import instrument_app
from src.observability.tracing import configure_tracing
from src.pipeline.registry import ConnectorRegistry
from src.pipeline.runner import QueryPipelineRunner

logger = logging.getLogger(__name__)

#: Must match the default in src/config.py and the compose fallback.
DEFAULT_JWT_SECRET = "dev-only-signing-key-not-for-production"


def _warn_on_default_secret(settings) -> None:
    """Say so, loudly, if the signing key is the one committed to the repo.

    `docker-compose.yml` supplies the same literal as its `${JWT_SECRET:-...}`
    fallback, so `make up` with no `.env` runs on a key that is public. That is
    fine for a demo and fatal anywhere else — the danger is that it happens
    silently. Warn rather than refuse, because refusing would break the
    one-command quickstart that is itself a submission gate.
    """
    if settings.JWT_SECRET == DEFAULT_JWT_SECRET:
        logger.warning(
            "JWT_SECRET is the default value committed to this repository. "
            "Every token this process issues or accepts is forgeable by anyone "
            "with the source. Set JWT_SECRET in .env for anything but a local demo."
        )


def _build_runner(settings, pool, redis_pipeline, repository, duckdb_pool) -> QueryPipelineRunner:
    """Assemble the pipeline once, here, and nowhere else.

    Every collaborator below is shared for the life of the process: one cache,
    one token bucket, one secrets client. Constructing them per request would
    give each request its own Redis script registration and would make the
    bucket's "per tenant, per connector" guarantee depend on which request
    happened to run — the governance primitives are only correct because they
    are shared.
    """
    cache = FreshnessCacheManager(redis_pipeline, ttl_ms=settings.CACHE_TTL_MS)
    limiter = TokenBucketRateLimiter(redis_pipeline)
    secrets = SecretsManagerClient(repository)
    # Wired ONLY in TEST_MODE, so outside it the forced-failure hook is not
    # merely refused by the route — it is absent from the fetch path entirely
    # and costs nothing per request. In test mode it lives in Redis rather than
    # in this process, because the image runs eight workers; see
    # src/governance/failure_hooks.py.
    failures = FailureHookStore(redis_pipeline) if settings.TEST_MODE else None
    registry = ConnectorRegistry(repository, cache, limiter, secrets, failures=failures)
    return QueryPipelineRunner(
        registry=registry,
        repository=repository,
        assembler=ResultAssembler(limiter, repository),
        audit=AuditLogger(pool),
        deadline_ms=settings.REQUEST_TIMEOUT_MS,
        duckdb_pool=duckdb_pool,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the backing stores, migrate, and expose both on ``app.state``."""
    settings = get_settings()
    _warn_on_default_secret(settings)

    pool = create_pool(settings.DATABASE_URL)
    applied = run_migrations(pool)
    logger.info("migrations applied: %s", applied or "none (schema current)")

    # TWO Redis clients, deliberately.
    #
    # `redis_client` is synchronous and serves the two places that must work
    # from a sync route: /healthz's ping and /v1/test/reset's flushdb. Making
    # those async would put a blocking psycopg call (healthz also checks
    # Postgres) onto the event loop, which is worse than a second connection.
    #
    # `redis_pipeline` is the async client every governance primitive uses —
    # the token bucket awaits its Lua script and the freshness cache awaits its
    # GET/SET. One extra connection per process is cheap; mixing sync calls into
    # the request path would not be.
    redis_client = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
    redis_client.ping()
    redis_pipeline = redis_async.from_url(settings.REDIS_URL, decode_responses=False)

    repository = ControlPlaneRepository(pool)

    # Named `duckdb_pool` because `pool` in this module is the Postgres pool.
    # Process-lived and bounded; see src/execution/duckdb_pool.py for why it is
    # per tenant rather than one shared instance.
    duckdb_pool = DuckDBPool(
        max_instances=settings.DUCKDB_MAX_INSTANCES,
        memory_limit=settings.DUCKDB_MEMORY_LIMIT,
    )

    app.state.pool = pool
    app.state.redis = redis_client
    app.state.repository = repository
    app.state.duckdb_pool = duckdb_pool
    app.state.runner = _build_runner(settings, pool, redis_pipeline, repository, duckdb_pool)

    try:
        yield
    finally:
        await redis_pipeline.aclose()
        redis_client.close()
        duckdb_pool.close()
        pool.close()


def create_app() -> FastAPI:
    """Build the application. Safe to call more than once (tests do)."""
    configure_logging()
    configure_tracing()

    app = FastAPI(
        title="Universal SQL",
        description="Federated SQL across enterprise apps, with query-time entitlement.",
        version="0.1.0",
        lifespan=lifespan,
    )

    install_error_handlers(app)
    # Added before the instrumentors so it sits INSIDE their spans and can read
    # the ambient trace_id — verified, not assumed (see test_logging.py).
    app.add_middleware(RequestLogMiddleware)
    app.include_router(router)

    # Order matters only in that both must happen before the first request.
    # Collectors only — the /metrics route belongs to routes.py.
    instrument_app(app)
    # Exclude the polled ops routes from tracing, for the same reason the access
    # log skips them: compose healthchecks every few seconds would otherwise make
    # the trace waterfall artifact almost entirely /healthz spans.
    #
    # `tracer_provider=` is not optional-but-tidy, it is load-bearing. Without
    # it the instrumentor resolves the OTel GLOBAL provider, which can only be
    # set once per process — so if anything set it before us, every server span
    # would be created by a provider we do not own and do not export from, and
    # `QueryEnvelope.trace_id` would silently stop correlating with the access
    # log and spans.jsonl. Handing it our own provider makes that impossible.
    FastAPIInstrumentor.instrument_app(
        app, excluded_urls="healthz,metrics", tracer_provider=configure_tracing()
    )

    return app


app = create_app()
