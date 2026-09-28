"""Fixtures for the hermetic suite.

`make test` and the pre-commit hook both run `pytest -q tests/unit`, so nothing
here may need a container. Redis comes from `fakeredis`, which runs the **real**
Lua script in-process; time comes from `FakeClock`. Both are wired here so the
convenient path in a test is also the correct one — a test that reached for
`time.time()` or a live Redis would work locally and fail in the hook.
"""

from collections.abc import Iterator

import fakeredis.aioredis
import pytest

from src.connectors.github import GitHubConnectorAdapter
from src.connectors.jira import JiraConnectorAdapter
from src.execution.assemble import ResultAssembler
from src.execution.duckdb_pool import DuckDBPool
from src.governance.cache import FreshnessCacheManager
from src.governance.clock import FakeClock
from src.governance.ratelimit import TokenBucketRateLimiter
from src.governance.secrets import SecretsManagerClient
from src.models.context import UserContext
from src.sqlparse.catalog import SourceCatalog
from src.sqlparse.parser import SQLParser
from tests.unit.catalog_fixture import (
    GITHUB_CAPABILITIES,
    GITHUB_ENDPOINT,
    GITHUB_RATE_LIMIT,
    JIRA_CAPABILITIES,
    JIRA_ENDPOINT,
    JIRA_RATE_LIMIT,
    build_catalog,
    connector_rows,
)
from tests.unit.fakes import (
    FakeControlPlane,
    SpyCache,
    SpyLimiter,
)

#: Re-exported: `test_whitelist` imports `build_catalog` from this module.
__all__ = [
    "GITHUB_CAPABILITIES",
    "GITHUB_ENDPOINT",
    "GITHUB_RATE_LIMIT",
    "JIRA_CAPABILITIES",
    "JIRA_ENDPOINT",
    "JIRA_RATE_LIMIT",
    "build_catalog",
    "connector_rows",
]


@pytest.fixture
def fake_clock() -> FakeClock:
    """A clock that only moves when the test moves it."""
    return FakeClock()


@pytest.fixture
async def fake_redis():
    """An in-process Redis that executes Lua, with no server.

    Function-scoped: bucket and cache state must not leak between tests, or a
    drain test would start from whatever the previous test left behind.
    """
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        yield redis
    finally:
        await redis.aclose()


# --- connector wiring -------------------------------------------------------
#
# The capability dicts below mirror `config/connectors/*.yaml`. They are
# repeated here rather than read from the YAML so the unit suite stays free of
# file I/O; `tests/integration/test_seed.py` is what asserts the YAML actually
# round-trips into these shapes through Postgres.


@pytest.fixture
def timeline() -> list[str]:
    """One ordered log across cache, limiter and secret store."""
    return []


@pytest.fixture
def control_plane(timeline) -> FakeControlPlane:
    return FakeControlPlane(timeline=timeline)


@pytest.fixture
def cache(fake_redis, fake_clock, timeline) -> SpyCache:
    return SpyCache(fake_redis, now_ms=fake_clock, ttl_ms=300_000, timeline=timeline)


@pytest.fixture
def limiter(fake_redis, fake_clock, timeline) -> SpyLimiter:
    return SpyLimiter(fake_redis, now_ms=fake_clock, timeline=timeline)


@pytest.fixture
def secrets(control_plane) -> SecretsManagerClient:
    return SecretsManagerClient(control_plane)


@pytest.fixture
def github(cache, limiter, secrets, control_plane, fake_clock) -> GitHubConnectorAdapter:
    return GitHubConnectorAdapter(
        resource="pull_requests",
        capabilities=GITHUB_CAPABILITIES,
        endpoint=GITHUB_ENDPOINT,
        rate_limit=GITHUB_RATE_LIMIT,
        cache=cache,
        limiter=limiter,
        secrets=secrets,
        control_plane=control_plane,
        now_ms=fake_clock,
    )


@pytest.fixture
def jira(cache, limiter, secrets, control_plane, fake_clock) -> JiraConnectorAdapter:
    return JiraConnectorAdapter(
        resource="issues",
        capabilities=JIRA_CAPABILITIES,
        endpoint=JIRA_ENDPOINT,
        rate_limit=JIRA_RATE_LIMIT,
        cache=cache,
        limiter=limiter,
        secrets=secrets,
        control_plane=control_plane,
        now_ms=fake_clock,
    )


@pytest.fixture
async def second_redis():
    """A second, independent Redis.

    Lets a test prove that two adapters with no shared state agree — an ETag
    compared across one cache is just a read-back of what was stored.
    """
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        yield redis
    finally:
        await redis.aclose()


@pytest.fixture
def second_github(second_redis, fake_clock, control_plane, secrets) -> GitHubConnectorAdapter:
    """An independent GitHub adapter: its own cache and its own bucket."""
    return GitHubConnectorAdapter(
        resource="pull_requests",
        capabilities=GITHUB_CAPABILITIES,
        endpoint=GITHUB_ENDPOINT,
        rate_limit=GITHUB_RATE_LIMIT,
        cache=FreshnessCacheManager(second_redis, now_ms=fake_clock, ttl_ms=300_000),
        limiter=TokenBucketRateLimiter(second_redis, now_ms=fake_clock),
        secrets=secrets,
        control_plane=control_plane,
        now_ms=fake_clock,
    )


# --- Phase 2: the parse/plan/execute fixtures -------------------------------
#
# The canonical query is a provenance rail (HLD §4 = design-doc §6.1). It is
# pinned here verbatim so a drift in any Phase 2 test is a one-line diff in one
# place rather than eight near-copies that slowly disagree.

CANONICAL_SQL = """
SELECT pr.title, pr.author, issue.key, issue.status
FROM   github.pull_requests pr
JOIN   jira.issues issue ON pr.issue_key = issue.key
WHERE  pr.repo = 'ema/core' AND pr.state = 'open' AND issue.status = 'In Progress'
ORDER BY issue.updated DESC
LIMIT 50
"""

#: The canonical query projects four columns and none is `reporter_email`, so it
#: cannot itself demonstrate CLS. This is the console's second preset (HLD §4).
CLS_DEMO_SQL = """
SELECT pr.title, pr.author, issue.key, issue.status, issue.reporter_email
FROM   github.pull_requests pr
JOIN   jira.issues issue ON pr.issue_key = issue.key
WHERE  pr.repo = 'ema/core' AND pr.state = 'open' AND issue.status = 'In Progress'
ORDER BY issue.updated DESC
LIMIT 50
"""

#: Both connectors granted, which is what `tenant_acme` is seeded with.
GRANTED = frozenset({"github", "jira"})

#: Re-exported: `test_whitelist` imports `build_catalog` from this module.
__all__ = [
    "GITHUB_CAPABILITIES",
    "GITHUB_ENDPOINT",
    "GITHUB_RATE_LIMIT",
    "JIRA_CAPABILITIES",
    "JIRA_ENDPOINT",
    "JIRA_RATE_LIMIT",
    "build_catalog",
    "connector_rows",
]


@pytest.fixture
def catalog() -> SourceCatalog:
    return build_catalog()


@pytest.fixture
def parser(catalog) -> SQLParser:
    return SQLParser(catalog)


@pytest.fixture
def alice() -> UserContext:
    """The headline persona: three entitled rows, `support` role."""
    return UserContext(
        tenant_id="tenant_acme",
        user_id="alice",
        roles=("support",),
        scopes=frozenset({"query:execute"}),
    )


def persona(user_id: str, *roles: str) -> UserContext:
    """Any persona on `tenant_acme`, for the tests that need bob/carol/auditor."""
    return UserContext(
        tenant_id="tenant_acme",
        user_id=user_id,
        roles=roles or ("support",),
        scopes=frozenset({"query:execute"}),
    )


@pytest.fixture
def adapters(github, jira) -> dict:
    """Both mock adapters, sharing one Redis so budgets and caches interact.

    Sharing is the realistic wiring — one process, one Redis — and it is what
    lets a test assert that a cache hit on one source did not spend the other's
    token.

    Keyed by ``(connector_type, resource)``, the way ``ConnectorRegistry.adapters()``
    keys them: one connector serves several API calls, so ``"github"`` alone no
    longer names an adapter.
    """
    return {
        ("github", "pull_requests"): github,
        ("jira", "issues"): jira,
    }


@pytest.fixture
def assembler(limiter, control_plane, fake_clock) -> ResultAssembler:
    """On the SAME injected clock as the cache the rows came from.

    Reading wall-clock time here while `fetched_at` came from `FakeClock` would
    make every fixture look hours stale and fire spurious STALE_DATA warnings.
    """
    return ResultAssembler(limiter, control_plane, fake_clock)


@pytest.fixture
def duckdb_pool() -> Iterator[DuckDBPool]:
    """A real pool, closed after the test.

    Real rather than faked: the pool IS the isolation boundary, so a stub that
    always handed back the same cursor would make every cross-tenant assertion
    in this suite pass for the wrong reason.
    """
    pool = DuckDBPool()
    try:
        yield pool
    finally:
        pool.close()
