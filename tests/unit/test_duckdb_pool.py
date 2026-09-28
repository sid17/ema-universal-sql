"""The pool, and the isolation properties the whole design rests on.

Phase 5 replaced `duckdb.connect(":memory:")` per request with one instance per
tenant. That is a **6.5ms -> 0.01ms** change on instance acquisition, and it is
only safe because `register()` is cursor-local. These tests assert that property
directly rather than trusting it: if a duckdb upgrade ever made registrations
instance-global, `test_two_cursors_on_one_instance_cannot_see_each_other` fails
and the design is caught before a tenant reads another tenant's rows.

Infra-free: duckdb is in-process, so this runs on the commit hook.
"""

from __future__ import annotations

import threading

import duckdb
import pyarrow as pa
import pytest

from src.execution.duckdb_pool import INSTANCE_CONFIG, DuckDBPool


def rows(cursor, sql: str):
    return cursor.execute(sql).fetchall()


@pytest.fixture
def pool():
    created = DuckDBPool()
    try:
        yield created
    finally:
        created.close()


# --- reuse: the reason this module exists -----------------------------------


def test_the_same_tenant_reuses_one_instance(pool) -> None:
    for _ in range(5):
        pool.cursor("tenant_acme").close()

    assert pool.stats() == {"live_instances": 1, "hits": 4, "misses": 1, "evictions": 0}


def test_each_tenant_gets_its_own_instance(pool) -> None:
    for tenant in ("tenant_acme", "tenant_globex", "tenant_load"):
        pool.cursor(tenant).close()

    assert pool.stats()["live_instances"] == 3
    assert pool.stats()["misses"] == 3


# --- isolation: the property the design rests on ----------------------------


def test_two_cursors_on_one_instance_cannot_see_each_other(pool) -> None:
    """`register()` is cursor-local. If this ever fails, the pool is unsafe.

    Two concurrent requests for the SAME tenant register the same view name with
    different rows. Each must see only its own — otherwise one user's result
    could be served from another user's registration, which is the entitlement
    leak the cache key exists to prevent, reintroduced one layer lower.
    """
    alice = pool.cursor("tenant_acme")
    bob = pool.cursor("tenant_acme")
    alice.register("issues", pa.table({"assignee": ["alice"]}))
    bob.register("issues", pa.table({"assignee": ["bob"]}))

    assert rows(alice, "SELECT assignee FROM issues") == [("alice",)]
    assert rows(bob, "SELECT assignee FROM issues") == [("bob",)]

    alice.close()
    bob.close()


def test_a_cursor_that_registered_nothing_raises_rather_than_reading_a_neighbour(
    pool,
) -> None:
    """The failure mode matters: not-found, never someone else's rows."""
    registered = pool.cursor("tenant_acme")
    registered.register("issues", pa.table({"assignee": ["alice"]}))
    bare = pool.cursor("tenant_acme")

    with pytest.raises(duckdb.CatalogException):
        rows(bare, "SELECT * FROM issues")

    registered.close()
    bare.close()


def test_a_registration_does_not_outlive_its_cursor(pool) -> None:
    """Otherwise request N+1 could read request N's leftover rows."""
    first = pool.cursor("tenant_acme")
    first.register("issues", pa.table({"assignee": ["alice"]}))
    first.close()

    second = pool.cursor("tenant_acme")
    with pytest.raises(duckdb.CatalogException):
        rows(second, "SELECT * FROM issues")
    second.close()


def test_one_tenant_cannot_read_another_tenants_registration(pool) -> None:
    acme = pool.cursor("tenant_acme")
    acme.register("issues", pa.table({"secret": ["acme-confidential"]}))
    globex = pool.cursor("tenant_globex")

    with pytest.raises(duckdb.CatalogException):
        rows(globex, "SELECT * FROM issues")

    acme.close()
    globex.close()


def test_concurrent_tenants_never_bleed(pool) -> None:
    """The load-shaped probe: 8 threads, 2 tenants, 800 interleaved joins.

    The join runs in `asyncio.to_thread`, so this is the real access pattern
    rather than a synthetic one. A single bleed here would mean the pool is
    unsafe under exactly the concurrency a load test produces.
    """
    bleeds: list[tuple[str, object]] = []
    errors: list[str] = []

    def worker(index: int) -> None:
        tenant = f"tenant_{index % 2}"
        try:
            for _ in range(100):
                cursor = pool.cursor(tenant)
                cursor.register("issues", pa.table({"who": [f"w{index}"]}))
                got = rows(cursor, "SELECT who FROM issues")
                cursor.close()
                if got != [(f"w{index}",)]:
                    bleeds.append((tenant, got))
        except Exception as exc:  # pragma: no cover - a failure here is the finding
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert bleeds == []


# --- the safety configuration -----------------------------------------------


def test_spilling_is_disabled_so_tenant_rows_never_reach_disk_in_plaintext(
    pool,
) -> None:
    """DuckDB's default is `temp_directory='.tmp'` with a 90%-of-disk cap, so an
    in-memory database writes tenant rows to disk in the clear under memory
    pressure. Disabled outright: too big must fail loudly (LAW 4)."""
    cursor = pool.cursor("tenant_acme")

    assert rows(cursor, "SELECT current_setting('temp_directory')") == [("",)]

    cursor.close()


def test_one_scheduler_thread_per_instance(pool) -> None:
    """The default is one thread per core, PER INSTANCE. With an instance per
    tenant and the join already in a thread pool, the default multiplies into
    hundreds of threads competing for a dozen cores."""
    cursor = pool.cursor("tenant_acme")

    assert rows(cursor, "SELECT current_setting('threads')") == [(1,)]
    assert INSTANCE_CONFIG["threads"] == 1

    cursor.close()


def test_a_memory_ceiling_is_applied_per_tenant() -> None:
    """So one tenant's oversized join cannot exhaust the process."""
    pool = DuckDBPool(memory_limit="128MB")
    cursor = pool.cursor("tenant_acme")
    try:
        setting = rows(cursor, "SELECT current_setting('memory_limit')")[0][0]
        assert "MiB" in setting or "MB" in setting
    finally:
        cursor.close()
        pool.close()


# --- the bound ---------------------------------------------------------------


def test_the_least_recently_used_instance_is_evicted() -> None:
    pool = DuckDBPool(max_instances=2)
    try:
        pool.cursor("a").close()
        pool.cursor("b").close()
        pool.cursor("a").close()  # `a` is now the most recent, so `b` is the victim
        pool.cursor("c").close()

        assert pool.stats()["evictions"] == 1
        assert pool.stats()["live_instances"] == 2
    finally:
        pool.close()


def test_an_evicted_instance_is_closed_not_leaked() -> None:
    pool = DuckDBPool(max_instances=1)
    evicted = pool._instance("a")
    pool.cursor("b").close()

    with pytest.raises(duckdb.ConnectionException):
        evicted.execute("SELECT 1")

    pool.close()


def test_a_pool_that_can_hold_nothing_is_a_configuration_error() -> None:
    """LAW 4: caught at construction, not as a confusing eviction loop later."""
    with pytest.raises(ValueError, match="max_instances must be >= 1"):
        DuckDBPool(max_instances=0)


def test_close_drains_every_instance() -> None:
    pool = DuckDBPool()
    pool.cursor("a").close()
    pool.cursor("b").close()

    pool.close()

    assert pool.stats()["live_instances"] == 0
