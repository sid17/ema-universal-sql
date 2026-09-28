"""One DuckDB instance per tenant, reused across requests.

**The measurement this module exists for.** Phase 4's load run found
``duckdb.connect(":memory:")`` costs **6.5ms** and the full register + execute +
read-back cycle **8.3ms** — so creating the instance was ~78% of the join stage
and roughly half of a cache-hit request. A cursor off a live instance costs
**0.01ms**, and the same full cycle drops to **~1.3ms**. The engine was never
slow; allocating a database per request was.

**Why per tenant, and not one instance for the whole process.** ``register()``
is cursor-local — measured, not assumed: two cursors on one instance registering
the same view name see only their own rows, a cursor that registered nothing
raises ``CatalogException``, and 8 threads interleaving 2,000 registrations
produced zero cross-tenant reads. So a single shared instance *would* be safe.
It is still not what this does: the isolation boundary should be the database,
not a convention about cursors, and a per-tenant instance is also what makes the
encrypted-materialization path (a tenant-keyed ``ATTACH``) land somewhere
natural rather than bolted on.

**Every instance is configured to never write plaintext to disk.** DuckDB's
defaults are ``temp_directory='.tmp'`` and ``max_temp_directory_size='90% of
available disk space'``, so an in-memory database spills tenant rows to disk in
the clear under memory pressure. Our datasets have never triggered it, but it is
a live path in running code rather than a hypothetical. ``temp_directory=''``
disables spilling outright, so a join too large for ``memory_limit`` fails
**loudly** instead of silently leaking (LAW 4).
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

import duckdb

logger = logging.getLogger(__name__)

#: Per-instance configuration, applied at connect time.
#:
#: ``threads=1`` because DuckDB defaults to one scheduler thread per core — 12
#: here — **per instance**. With an instance per tenant and the join already
#: running in the app's own thread pool, the default would multiply into
#: hundreds of scheduler threads competing for 12 cores. Our inputs are
#: hundreds of rows, where DuckDB's intra-query parallelism buys nothing.
#:
#: ``temp_directory=''`` hard-disables spilling. See the module docstring.
INSTANCE_CONFIG: dict[str, str | int] = {
    "threads": 1,
    "temp_directory": "",
}

#: Per-tenant memory ceiling, so one tenant's oversized join cannot exhaust the
#: process and take every other tenant down with it. Paired with the disabled
#: temp directory this makes "too big" a loud, contained failure.
DEFAULT_MEMORY_LIMIT = "256MB"

#: How many tenants keep a warm instance per process. Measured footprint is
#: ~2.5 MiB per idle instance, so 32 costs ~80 MiB per worker. Eviction closes
#: the instance; the next request for that tenant simply pays the 6.5ms again.
DEFAULT_MAX_INSTANCES = 32


class DuckDBPool:
    """A bounded, thread-safe LRU of per-tenant DuckDB instances.

    Accessed from the thread the join runs on (``asyncio.to_thread``), so the
    bookkeeping is guarded by a lock. The lock covers **only** the dictionary —
    never query execution — so two tenants still join concurrently.
    """

    def __init__(
        self,
        max_instances: int = DEFAULT_MAX_INSTANCES,
        memory_limit: str = DEFAULT_MEMORY_LIMIT,
    ) -> None:
        if max_instances < 1:
            raise ValueError(f"max_instances must be >= 1, got {max_instances}")
        self._max_instances = max_instances
        self._memory_limit = memory_limit
        self._instances: OrderedDict[str, duckdb.DuckDBPyConnection] = OrderedDict()
        self._lock = threading.Lock()
        #: Observability, not bookkeeping: lets a load run report how often the
        #: LRU actually evicted rather than leaving it to inference.
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def cursor(self, tenant_id: str) -> duckdb.DuckDBPyConnection:
        """A fresh cursor on *this tenant's* instance.

        The caller owns the cursor and must close it. Registrations made on it
        are visible to it alone, so concurrent requests for the same tenant do
        not see each other's tables.
        """
        return self._instance(tenant_id).cursor()

    def _instance(self, tenant_id: str) -> duckdb.DuckDBPyConnection:
        with self._lock:
            existing = self._instances.get(tenant_id)
            if existing is not None:
                self._instances.move_to_end(tenant_id)
                self.hits += 1
                return existing

            self.misses += 1
            created = self._connect()
            self._instances[tenant_id] = created
            self._evict_if_over_capacity()
            return created

    def _connect(self) -> duckdb.DuckDBPyConnection:
        connection = duckdb.connect(":memory:", config=dict(INSTANCE_CONFIG))
        # Set after connect rather than in `config`: an invalid memory_limit is
        # then a clear error on a statement we can name, not an opaque failure
        # inside the constructor.
        connection.execute(f"SET memory_limit='{self._memory_limit}'")
        return connection

    def _evict_if_over_capacity(self) -> None:
        """Close and drop least-recently-used instances. Caller holds the lock."""
        while len(self._instances) > self._max_instances:
            tenant_id, connection = self._instances.popitem(last=False)
            self.evictions += 1
            logger.info("duckdb_pool: evicting instance for tenant %s", tenant_id)
            # LAW 4: a close that fails is logged with its traceback. It is not
            # re-raised, because the eviction is a side effect of another
            # tenant's request and failing that request for it would be wrong —
            # but it must never pass silently.
            try:
                connection.close()
            except Exception:
                logger.exception("duckdb_pool: closing evicted instance for %s", tenant_id)

    def stats(self) -> dict[str, int]:
        """Counters for the load report. Cheap enough to call per scrape."""
        with self._lock:
            return {
                "live_instances": len(self._instances),
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
            }

    def close(self) -> None:
        """Close every instance. Called on application shutdown."""
        with self._lock:
            while self._instances:
                tenant_id, connection = self._instances.popitem(last=False)
                try:
                    connection.close()
                except Exception:
                    logger.exception("duckdb_pool: closing instance for %s", tenant_id)
