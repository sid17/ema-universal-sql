"""The runtime read layer over the control plane, with an in-process TTL cache.

The control plane is read at request time and cached. The cache is what makes
that affordable: without it every query pays four or five Postgres round-trips
before it touches a connector, and the measured P95 ends up describing Postgres
instead of the slow source.

Six reads, and who consumes each:

===================================  ======================================
Method                               Consumer
===================================  ======================================
``get_tenant``                       the tenant-status gate (``deps.py``)
``get_rate_limit_policy``            ``TokenBucketRateLimiter`` sizing
``get_tenant_connectors``            the ``CONNECTOR_NOT_ENABLED`` gate
``list_connectors``                  ``ConnectorRegistry`` catalog+adapters
``get_connector``                    one resource's endpoint + capabilities
``get_policies``                     ``EntitlementEngine``
===================================  ======================================

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

    Typed because there is a real consumer reading ``status``. The other
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

        The grant lives on ``tenant_connector``; the connector's own
        definition lives on the global ``connectors`` catalog.
        """

        def load() -> list[dict[str, Any]]:
            return self._query(
                "SELECT connector_type, enabled, status, secret_ref "
                "FROM tenant_connector WHERE tenant_id = %s",
                (tenant_id,),
            )

        return self._cache.get_or_load(("tenant_connectors", tenant_id), load)

    #: Columns every connector read selects. One list, so a caller cannot get a
    #: row from :meth:`get_connector` shaped differently from :meth:`list_connectors`.
    _CONNECTOR_COLUMNS = "connector_type, resource, version, endpoint, rate_limit, capabilities"

    def list_connectors(self) -> list[dict[str, Any]]:
        """Every seeded ``(connector_type, resource)`` — one row per API call.

        The registry builds the catalog and the adapters from **these rows**,
        not from a list of Python classes. That is what makes a second GitHub
        endpoint one more entry in ``config/connectors/github.yaml`` and no code
        at all: the class is selected by ``connector_type``, everything else
        about the call comes from here.
        """

        def load() -> list[dict[str, Any]]:
            return self._query(
                f"SELECT {self._CONNECTOR_COLUMNS} FROM connectors "
                "ORDER BY connector_type, resource"
            )

        return self._cache.get_or_load(("connectors",), load)

    def get_connector(self, connector_type: str, resource: str) -> dict[str, Any] | None:
        """One connector resource: its endpoint, its rate-limit dialect, its capabilities.

        Global, not per-tenant: all three describe the upstream API, and two
        tenants querying the same resource get the same pushdown options.
        """

        def load() -> dict[str, Any] | None:
            rows = self._query(
                f"SELECT {self._CONNECTOR_COLUMNS} FROM connectors "
                "WHERE connector_type = %s AND resource = %s",
                (connector_type, resource),
            )
            return rows[0] if rows else None

        return self._cache.get_or_load(("connector", connector_type, resource), load)

    def get_policies(
        self,
        tenant_id: str,
        connectors: Iterable[str],
        resources: Iterable[str],
    ) -> list[dict[str, Any]]:
        """**Every** enabled policy on the resources this query references.

        Scoped by tenant and by resource — but deliberately **not by role**.

        This read used to carry ``AND (applies_to = ANY(roles) OR applies_to =
        '*')``, and that was a fail-open bug rather than an optimization.
        Default-deny asks *"is this resource governed by a policy that does not
        match me?"*, and a role-filtered read cannot answer it: a caller whose
        roles match nothing gets an empty list, the engine concludes the
        resource is ungoverned, and the query runs **unrestricted**. The caller
        with no grant at all is exactly the caller who would be handed every
        row. It was caught by ``test_default_deny_is_empty`` returning the full
        8-row unfiltered baseline.

        So role matching lives in one place — :func:`src.entitlement.engine.applies_to`
        — where it is an authorization decision that can be read, reviewed and
        tested, rather than half here in a SQL ``WHERE`` clause and half there.
        The policy set for one tenant is a handful of rows; filtering them in
        Python costs nothing.
        """
        connector_key = tuple(sorted(connectors))
        resource_key = tuple(sorted(resources))

        def load() -> list[dict[str, Any]]:
            return self._query(
                "SELECT policy_id, connector_type, resource, kind, applies_to, effect, "
                "       predicate, column_name, mask, version "
                "FROM policies "
                "WHERE tenant_id = %s AND enabled = true "
                "  AND connector_type = ANY(%s) AND resource = ANY(%s)",
                (tenant_id, list(connector_key), list(resource_key)),
            )

        return self._cache.get_or_load(("policies", tenant_id, connector_key, resource_key), load)

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

    def get_secret(self, secret_ref: str) -> dict[str, Any] | None:
        """The encrypted secret behind a ``tenant_connector.secret_ref``.

        **Deliberately not cached**, unlike every other read on this class. The
        other reads are policy and capability metadata; this one is ciphertext
        whose plaintext is a credential. Holding it in a process-local dictionary
        for the control-plane TTL would widen the window in which a heap dump
        exposes it, and would keep serving a secret that had already been rotated
        or revoked — the opposite of what rotation is for. The read is a single
        indexed primary-key lookup, so caching buys very little anyway.

        Returns the row including ``tenant_id``: the owning tenant comes from
        the stored row, never from the caller, so a caller cannot ask for one
        tenant's ciphertext to be decrypted with another tenant's key.
        """
        rows = self._query(
            "SELECT secret_ref, tenant_id, ciphertext FROM secrets WHERE secret_ref = %s",
            (secret_ref,),
        )
        return rows[0] if rows else None
