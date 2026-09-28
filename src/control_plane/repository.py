"""The runtime read layer over the control plane, with an in-process TTL cache.

HLD §3 describes the control plane as *"read at request time (cached)"*. The
cache is what makes that true rather than aspirational: without it every query
pays four or five Postgres round-trips before it touches a connector, and the
Phase 4 P95 ends up measuring Postgres instead of Jira — which is precisely the
thing the trace is supposed to disprove.

Three later phases read through this class, so all five reads exist here even
though Phase 0 only consumes :meth:`ControlPlaneRepository.get_tenant`:

===================================  ======================================  =====
Method                               Consumer                                Phase
===================================  ======================================  =====
``get_tenant``                       the tenant-status gate (``deps.py``)    P0
``get_rate_limit_policy``            ``TokenBucketRateLimiter`` sizing       P1
``get_tenant_connectors``            the ``CONNECTOR_NOT_ENABLED`` gate      P2
``get_capabilities``                 ``QueryPlanner`` capability check       P2
``get_policies``                     ``EntitlementEngine``                   P2
===================================  ======================================  =====

The cache is per-process and deliberately unsynchronised across replicas: a
policy change becomes visible within ``CONTROL_PLANE_TTL_MS`` everywhere, which
is the same staleness contract the real design gives its config plane.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from psycopg.rows import dict_row

from src.config import get_settings


@dataclass(frozen=True)
class Tenant:
    """A tenant as the gate needs it.

    Typed because Phase 0 has a real consumer reading ``status``. The other
    reads return plain rows until the phase that consumes them shapes them.
    """

    tenant_id: str
    name: str
    status: str
    residency: str
    deployment_mode: str
    fernet_key: str

    @property
    def is_active(self) -> bool:
        """``suspended`` and ``offboarding`` tenants are refused before planning."""
        return self.status == "active"


#: Cache ceiling. The real key space is a handful of tenants, but the key is
#: derived from `tenant_id` in the caller's JWT — and in this prototype
#: `POST /v1/auth/mock-token` mints a token for ANY tenant string without
#: authenticating. An unbounded dict is therefore attacker-growable: a loop of
#: requests naming fresh tenant ids pins one cache entry each, forever, since
#: misses are cached too. Bounded + LRU makes that a no-op instead of a leak.
MAX_CACHE_ENTRIES = 1024


class _TTLCache:
    """A small monotonic-clock cache, bounded and LRU-evicting.

    LRU rather than "not LRU — the key space is a few tenants", which was the
    earlier assumption: the key space is whatever a caller puts in a token.
    """

    def __init__(self, ttl_ms: int, max_entries: int = MAX_CACHE_ENTRIES) -> None:
        self._ttl_s = ttl_ms / 1000.0
        self._max_entries = max_entries
        self._entries: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get_or_load(self, key: Any, loader: Callable[[], Any]) -> Any:
        now = time.monotonic()
        hit = self._entries.get(key)
        if hit is not None and now - hit[0] < self._ttl_s:
            self._entries.move_to_end(key)
            return hit[1]

        value = loader()
        self._entries[key] = (now, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
        return value

    def invalidate(self) -> None:
        self._entries.clear()


class ControlPlaneRepository:
    """Cached reads over the seeded control-plane tables."""

    def __init__(self, pool: Any, ttl_ms: int | None = None) -> None:
        self._pool = pool
        if ttl_ms is None:
            ttl_ms = get_settings().CONTROL_PLANE_TTL_MS
        self._cache = _TTLCache(ttl_ms)

    # -- plumbing ---------------------------------------------------------

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Run one parameterised read. Params are always bound, never formatted in."""
        # row_factory on the CURSOR, not the connection: a connection goes back
        # to the pool for the next borrower, so mutating it leaks this module's
        # preference into unrelated callers.
        with (
            self._pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            return cur.execute(sql, params).fetchall()

    def invalidate(self) -> None:
        """Drop every cached read. Used by ``POST /v1/test/reset``.

        Without this, a test that re-seeds the database would keep serving the
        previous seed for up to the TTL and fail for reasons that look like
        application bugs.
        """
        self._cache.invalidate()

    # -- reads ------------------------------------------------------------

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        """Return the tenant, or ``None`` when it does not exist."""

        def load() -> Tenant | None:
            rows = self._query(
                "SELECT tenant_id, name, status, residency, deployment_mode, fernet_key "
                "FROM tenants WHERE tenant_id = %s",
                (tenant_id,),
            )
            return Tenant(**rows[0]) if rows else None

        return self._cache.get_or_load(("tenant", tenant_id), load)

    def get_tenant_connectors(self, tenant_id: str) -> list[dict[str, Any]]:
        """The connectors this tenant is granted, with their ``secret_ref``.

        ADR-013: the grant lives on ``tenant_connector``; the connector's own
        definition lives on the global ``connectors`` catalog.
        """

        def load() -> list[dict[str, Any]]:
            return self._query(
                "SELECT connector_type, enabled, status, secret_ref "
                "FROM tenant_connector WHERE tenant_id = %s",
                (tenant_id,),
            )

        return self._cache.get_or_load(("tenant_connectors", tenant_id), load)

    def get_capabilities(self, connector_type: str) -> dict[str, Any] | None:
        """The connector's capability model — what it can be asked to filter on.

        Global, not per-tenant: capabilities describe the upstream API, and two
        tenants querying the same connector get the same pushdown options.
        """

        def load() -> dict[str, Any] | None:
            rows = self._query(
                "SELECT connector_type, version, capabilities "
                "FROM connectors WHERE connector_type = %s",
                (connector_type,),
            )
            return rows[0] if rows else None

        return self._cache.get_or_load(("capabilities", connector_type), load)

    def get_policies(
        self,
        tenant_id: str,
        connectors: Iterable[str],
        resources: Iterable[str],
        roles: Iterable[str],
    ) -> list[dict[str, Any]]:
        """Enabled RLS/CLS policies matching this query's scope and the caller's roles.

        ``applies_to = '*'`` is always included alongside the caller's named
        roles. Deny-overrides and default-deny are the *engine's* job (ADR-010);
        this read only narrows the candidate set.
        """
        connector_key = tuple(sorted(connectors))
        resource_key = tuple(sorted(resources))
        role_key = tuple(sorted(roles))

        def load() -> list[dict[str, Any]]:
            return self._query(
                "SELECT policy_id, connector_type, resource, kind, applies_to, effect, "
                "       predicate, column_name, mask, version "
                "FROM policies "
                "WHERE tenant_id = %s AND enabled = true "
                "  AND connector_type = ANY(%s) AND resource = ANY(%s) "
                "  AND (applies_to = ANY(%s) OR applies_to = '*')",
                (tenant_id, list(connector_key), list(resource_key), list(role_key)),
            )

        return self._cache.get_or_load(
            ("policies", tenant_id, connector_key, resource_key, role_key), load
        )

    def get_rate_limit_policy(self, tenant_id: str, connector_type: str) -> dict[str, Any] | None:
        """The token-bucket sizing for this tenant against this connector.

        Deliberately per-tenant: ``tenant_acme``'s GitHub budget is tiny so the
        429 demo is deterministic, while ``tenant_load`` is large because it is
        the only tenant k6 may target.
        """

        def load() -> dict[str, Any] | None:
            rows = self._query(
                "SELECT max_requests, window_sec, burst FROM rate_limit_policies "
                "WHERE tenant_id = %s AND connector_type = %s",
                (tenant_id, connector_type),
            )
            return rows[0] if rows else None

        return self._cache.get_or_load(("rate_limit", tenant_id, connector_type), load)
