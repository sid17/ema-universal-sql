"""The control-plane cache: the thing that keeps Postgres off the hot path.

The central assertion is a **query count**, not a return value. A repository that
returns the right row every time but re-queries on every call would pass any
value-based test while making the Phase 4 P95 measure Postgres instead of Jira —
which is exactly what the trace artifact is supposed to disprove.

Infra-free: a fake pool stands in for psycopg and counts what was asked of it.
"""

from contextlib import contextmanager
from typing import Any

import pytest

from src.control_plane.repository import (
    MAX_CACHE_ENTRIES,
    ControlPlaneRepository,
    Tenant,
)

TENANT_ROW = {
    "tenant_id": "tenant_acme",
    "name": "Acme",
    "status": "active",
    "residency": "us",
    "deployment_mode": "multi-tenant",
    "fernet_key": "not-a-real-key",
}


class FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class FakeConnection:
    """Mirrors the psycopg 3 surface the repository actually uses.

    Deliberately offers `cursor(row_factory=...)` and NOT a settable
    `row_factory` on the connection — the real pool hands the same connection to
    the next borrower, so a double that tolerated connection-level mutation
    would hide exactly that bug.
    """

    def __init__(self, pool: "FakePool") -> None:
        self._pool = pool

    @contextmanager
    def cursor(self, row_factory: Any = None):
        yield FakeCursorFactory(self._pool)


class FakeCursorFactory:
    def __init__(self, pool: "FakePool") -> None:
        self._pool = pool

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> FakeCursor:
        self._pool.queries.append((sql, params))
        return FakeCursor(self._pool.rows)


class FakePool:
    """Counts every query the repository issues."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows if rows is not None else [dict(TENANT_ROW)]
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    @contextmanager
    def connection(self):
        yield FakeConnection(self)

    @property
    def count(self) -> int:
        return len(self.queries)


@pytest.fixture
def pool() -> FakePool:
    return FakePool()


@pytest.fixture
def repo(pool: FakePool) -> ControlPlaneRepository:
    return ControlPlaneRepository(pool, ttl_ms=30_000)


# --------------------------------------------------------------------------
# The cache contract
# --------------------------------------------------------------------------


def test_second_call_inside_the_ttl_does_not_hit_postgres(repo, pool):
    first = repo.get_tenant("tenant_acme")
    second = repo.get_tenant("tenant_acme")

    assert pool.count == 1, "the second read should have been served from cache"
    assert first == second


def test_invalidate_forces_a_reread(repo, pool):
    """`POST /v1/test/reset` re-seeds; without this the old seed lingers."""
    repo.get_tenant("tenant_acme")
    repo.invalidate()
    repo.get_tenant("tenant_acme")

    assert pool.count == 2


def test_expiry_past_the_ttl_forces_a_reread(pool):
    expiring = ControlPlaneRepository(pool, ttl_ms=0)

    expiring.get_tenant("tenant_acme")
    expiring.get_tenant("tenant_acme")

    assert pool.count == 2


def test_different_tenants_are_cached_separately(repo, pool):
    """A shared cache slot would leak one tenant's config into another's request."""
    repo.get_tenant("tenant_acme")
    repo.get_tenant("tenant_globex")
    repo.get_tenant("tenant_acme")

    assert pool.count == 2
    assert pool.queries[0][1] == ("tenant_acme",)
    assert pool.queries[1][1] == ("tenant_globex",)


def test_each_read_has_its_own_cache_slot(repo, pool):
    """Five reads, five queries — no key collisions between different methods."""
    repo.get_tenant("tenant_acme")
    repo.get_tenant_connectors("tenant_acme")
    repo.get_capabilities("github")
    repo.get_policies("tenant_acme", ["github"], ["pull_requests"], ["support"])
    repo.get_rate_limit_policy("tenant_acme", "github")

    assert pool.count == 5

    # ...and every one of them is then cached.
    repo.get_tenant("tenant_acme")
    repo.get_tenant_connectors("tenant_acme")
    repo.get_capabilities("github")
    repo.get_policies("tenant_acme", ["github"], ["pull_requests"], ["support"])
    repo.get_rate_limit_policy("tenant_acme", "github")

    assert pool.count == 5


def test_policy_cache_key_is_order_independent(repo, pool):
    """The same scope in a different order is the same query, not a cache miss."""
    repo.get_policies("tenant_acme", ["github", "jira"], ["pull_requests"], ["support"])
    repo.get_policies("tenant_acme", ["jira", "github"], ["pull_requests"], ["support"])

    assert pool.count == 1


def test_policy_cache_distinguishes_roles(repo, pool):
    """Two callers with different roles must not share an entitlement result."""
    repo.get_policies("tenant_acme", ["jira"], ["issues"], ["support"])
    repo.get_policies("tenant_acme", ["jira"], ["issues"], ["admin"])

    assert pool.count == 2


# --------------------------------------------------------------------------
# What the reads return
# --------------------------------------------------------------------------


def test_get_tenant_returns_a_typed_tenant(repo):
    tenant = repo.get_tenant("tenant_acme")

    assert isinstance(tenant, Tenant)
    assert tenant.tenant_id == "tenant_acme"
    assert tenant.is_active


@pytest.mark.parametrize("status", ["suspended", "offboarding"])
def test_non_active_tenants_are_not_active(status):
    pool = FakePool([{**TENANT_ROW, "status": status}])
    tenant = ControlPlaneRepository(pool).get_tenant("tenant_acme")

    assert not tenant.is_active


def test_missing_tenant_returns_none():
    pool = FakePool([])
    assert ControlPlaneRepository(pool).get_tenant("tenant_nope") is None


def test_missing_rows_are_cached_too():
    """A negative result must cache, or an unknown tenant re-queries every request."""
    pool = FakePool([])
    repo = ControlPlaneRepository(pool)

    repo.get_tenant("tenant_nope")
    repo.get_tenant("tenant_nope")

    assert pool.count == 1


def test_policies_read_scopes_to_tenant_and_enabled(repo, pool):
    repo.get_policies("tenant_acme", ["jira"], ["issues"], ["support"])
    sql, params = pool.queries[0]

    assert "enabled = true" in sql
    assert params[0] == "tenant_acme"
    assert "applies_to = '*'" in sql, "wildcard policies must always be candidates"


# --------------------------------------------------------------------------
# The cache is bounded (found in review)
# --------------------------------------------------------------------------


def test_cache_is_bounded_and_evicts_lru(pool):
    """The key derives from `tenant_id` in a JWT, and this prototype mints a
    token for ANY tenant string without authenticating. An unbounded cache is
    therefore attacker-growable — misses are cached too, so a loop of fresh
    tenant ids would pin one entry each, forever.
    """
    repo = ControlPlaneRepository(pool, ttl_ms=30_000)

    for i in range(MAX_CACHE_ENTRIES * 2):
        repo.get_tenant(f"tenant_{i}")

    assert len(repo._cache._entries) <= MAX_CACHE_ENTRIES


def test_eviction_is_least_recently_used(pool):
    """The hot tenant must survive a flood of one-shot lookups."""
    repo = ControlPlaneRepository(pool, ttl_ms=30_000)

    repo.get_tenant("tenant_hot")
    for i in range(MAX_CACHE_ENTRIES):
        repo.get_tenant(f"tenant_cold_{i}")
        repo.get_tenant("tenant_hot")  # keep it recently used

    before = pool.count
    repo.get_tenant("tenant_hot")
    assert pool.count == before, "the hot tenant was evicted despite constant use"
