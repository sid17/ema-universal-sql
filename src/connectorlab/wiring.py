"""One live wiring, borrowed from the running stack.

Deliberately the **same** collaborators the app uses — the seeded control plane,
the real Redis token bucket, the real freshness cache. A lab wired to fakes
would prove that the fakes agree with each other.

That is why this runs inside the app container (``make connectors``), the way
``make seed`` does: Postgres and Redis publish no host ports.
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import redis.asyncio as redis_async

from src.config import get_settings
from src.connectors.base import BaseConnectorAdapter
from src.control_plane.db import create_pool
from src.control_plane.repository import ControlPlaneRepository
from src.governance.cache import FreshnessCacheManager
from src.governance.ratelimit import TokenBucketRateLimiter
from src.governance.secrets import SecretsManagerClient
from src.pipeline.registry import ConnectorRegistry

#: The tenant the walkthrough queries, and it is NOT the demo tenant.
#:
#: `tenant_acme` has a GitHub budget of 5+2, sized so `make demo`'s 429 is
#: deterministic. The walkthrough makes a dozen live fetches — paging alone
#: spends one token per page — so running it against acme drained that bucket
#: and the next `make demo` would have opened with a 429 instead of a result.
#: An artifact generator that quietly breaks another artifact is worse than one
#: that prints less.
#:
#: `tenant_load` is the k6 tenant: 5000/60s on both connectors, and grants for
#: both. The rate-limit scene needs the opposite and uses a third tenant — see
#: `scenes.THROTTLE_TENANT`.
DEFAULT_TENANT = "tenant_load"

#: Not a real caller's scope. It is part of the cache key, and using a
#: recognisable one keeps the lab's cache entries out of the demo's.
DEFAULT_SCOPE = "connectorlab:support"


@dataclass
class Lab:
    """Everything a scene needs: the seeded rows and the built adapters."""

    registry: ConnectorRegistry
    adapters: dict[tuple[str, str], BaseConnectorAdapter]
    rows: list[dict[str, Any]]

    def adapter(self, qualified: str) -> BaseConnectorAdapter:
        """Look one up by ``github.pull_requests``, refusing anything unseeded."""
        connector, _, resource = qualified.partition(".")
        found = self.adapters.get((connector, resource))
        if found is None:
            known = ", ".join(f"{c}.{r}" for c, r in sorted(self.adapters))
            raise SystemExit(f"unknown source {qualified!r}; seeded: {known}")
        return found


@asynccontextmanager
async def open_lab():
    """Open the same clients the app opens, and close them on the way out."""
    settings = get_settings()
    pool = create_pool(settings.DATABASE_URL)
    redis = redis_async.from_url(settings.REDIS_URL, decode_responses=False)
    try:
        repository = ControlPlaneRepository(pool)
        registry = ConnectorRegistry(
            repository,
            FreshnessCacheManager(redis, ttl_ms=settings.CACHE_TTL_MS),
            TokenBucketRateLimiter(redis),
            SecretsManagerClient(repository),
        )
        rows = repository.list_connectors()
        if not rows:
            raise SystemExit(
                "the control plane has no connectors. Run `make seed` first — "
                "`make up` starts the app before the catalog is loaded."
            )
        yield Lab(registry=registry, adapters=await registry.adapters(), rows=rows)
    finally:
        await redis.aclose()
        pool.close()
