"""Fixtures for the hermetic suite.

`make test` and the pre-commit hook both run `pytest -q tests/unit`, so nothing
here may need a container. Redis comes from `fakeredis`, which runs the **real**
Lua script in-process; time comes from `FakeClock`. Both are wired here so the
convenient path in a test is also the correct one — a test that reached for
`time.time()` or a live Redis would work locally and fail in the hook.
"""

from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from cryptography.fernet import Fernet

from src.connectors.base import CapabilityModel
from src.connectors.github import GitHubConnectorAdapter
from src.connectors.jira import JiraConnectorAdapter
from src.governance.cache import FreshnessCacheManager
from src.governance.clock import FakeClock
from src.governance.ratelimit import TokenBucketRateLimiter
from src.governance.secrets import SecretsManagerClient
from src.models.context import UserContext
from src.sqlparse.catalog import Source, SourceCatalog
from src.sqlparse.parser import SQLParser


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

GITHUB_CAPABILITIES = {
    "columns": [
        "number", "title", "author", "repo", "state", "issue_key",
        "created_at", "updated_at",
    ],
    "key_columns": {
        "repo": {
            "require": "required",
            "ops": ["="],
            "option": {"inject_into": "path", "field": "repo"},
        },
        "state": {
            "require": "optional",
            "ops": ["="],
            "option": {"inject_into": "query", "field": "state"},
        },
        "author": {
            "require": "optional",
            "ops": ["="],
            "option": {"inject_into": "query", "field": "author"},
        },
    },
    "column_types": {"number": "integer"},
    "sortable": ["created_at", "updated_at"],
    "pagination": {
        "strategy": "cursor",
        "page_size": 100,
        "token_option": {"inject_into": "query", "field": "cursor"},
        "stop": "returned<page_size",
    },
}

JIRA_CAPABILITIES = {
    "columns": ["key", "status", "assignee", "reporter_email", "project", "updated"],
    "key_columns": {
        "status": {
            "require": "optional",
            "ops": ["="],
            "option": {"inject_into": "query", "field": "status"},
        },
        "assignee": {
            "require": "optional",
            "ops": ["="],
            "option": {"inject_into": "query", "field": "assignee"},
        },
        "project": {
            "require": "optional",
            "ops": ["="],
            "option": {"inject_into": "query", "field": "project"},
        },
        "updated": {
            "require": "optional",
            "ops": ["=", ">", ">=", "<", "<="],
            "option": {"inject_into": "query", "field": "updated"},
        },
    },
    "sortable": ["updated"],
    "pagination": {
        "strategy": "offset",
        "page_size": 100,
        "token_option": {"inject_into": "query", "field": "startAt"},
        "stop": "returned<page_size",
    },
}

ACME_FERNET_KEY = Fernet.generate_key().decode()


class FakeControlPlane:
    """The four control-plane reads a connector makes, without Postgres.

    Counts its calls so a test can assert that an adapter which served from
    cache did not go on to read a rate-limit policy it had no use for.
    """

    def __init__(self, timeline: list[str] | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        # Shared with SpyLimiter so a test can assert the ORDER of steps across
        # both collaborators, not just that each one happened.
        self.timeline = [] if timeline is None else timeline
        self.rate_limits = {
            ("tenant_acme", "github"): {"max_requests": 5, "window_sec": 60, "burst": 2},
            ("tenant_acme", "jira"): {"max_requests": 30, "window_sec": 60, "burst": 5},
            ("tenant_load", "github"): {"max_requests": 5000, "window_sec": 60, "burst": 500},
            ("tenant_load", "jira"): {"max_requests": 5000, "window_sec": 60, "burst": 500},
        }
        self.grants = {
            "tenant_acme": [
                {"connector_type": "github", "enabled": True, "status": "active",
                 "secret_ref": "tenant_acme/github"},
                {"connector_type": "jira", "enabled": True, "status": "active",
                 "secret_ref": "tenant_acme/jira"},
            ],
            "tenant_load": [
                {"connector_type": "github", "enabled": True, "status": "active",
                 "secret_ref": "tenant_load/github"},
                {"connector_type": "jira", "enabled": True, "status": "active",
                 "secret_ref": "tenant_load/jira"},
            ],
        }
        self.secrets = {
            ref: {
                "secret_ref": ref,
                "tenant_id": ref.split("/")[0],
                "ciphertext": SecretsManagerClient.encrypt(ACME_FERNET_KEY, f"token-for-{ref}"),
            }
            for ref in (
                "tenant_acme/github", "tenant_acme/jira",
                "tenant_load/github", "tenant_load/jira",
            )
        }
        self.tenants = {
            t: SimpleNamespace(tenant_id=t, fernet_key=ACME_FERNET_KEY)
            for t in ("tenant_acme", "tenant_load")
        }

    def get_rate_limit_policy(self, tenant_id, connector_type):
        self.calls.append(("get_rate_limit_policy", (tenant_id, connector_type)))
        return self.rate_limits.get((tenant_id, connector_type))

    def read_cache_marker(self) -> None:
        """Called by the cache spy — see `cache` fixture."""
        self.timeline.append("read_cache")

    def get_tenant_connectors(self, tenant_id):
        self.calls.append(("get_tenant_connectors", (tenant_id,)))
        return self.grants.get(tenant_id, [])

    def get_secret(self, secret_ref):
        self.calls.append(("get_secret", (secret_ref,)))
        self.timeline.append("resolve_secret")
        return self.secrets.get(secret_ref)

    def get_tenant(self, tenant_id):
        self.calls.append(("get_tenant", (tenant_id,)))
        return self.tenants.get(tenant_id)


class SpyLimiter(TokenBucketRateLimiter):
    """A limiter that records every consume, so ORDER can be asserted.

    The other tests assert outcomes; an outcome cannot distinguish "cache hit,
    no token spent" from "token spent, then refunded".
    """

    def __init__(self, *args, timeline: list[str] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.consumed: list[tuple[str, str]] = []
        self.timeline = [] if timeline is None else timeline

    async def consume(self, tenant_id, connector_type, policy):
        self.consumed.append((tenant_id, connector_type))
        self.timeline.append("consume_token")
        return await super().consume(tenant_id, connector_type, policy)


class SpyCache(FreshnessCacheManager):
    """Records cache reads onto the shared timeline."""

    def __init__(self, *args, timeline: list[str] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.timeline = [] if timeline is None else timeline

    async def get(self, key, max_staleness_ms):
        self.timeline.append("read_cache")
        return await super().get(key, max_staleness_ms)


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
        capabilities=GITHUB_CAPABILITIES,
        cache=cache,
        limiter=limiter,
        secrets=secrets,
        control_plane=control_plane,
        now_ms=fake_clock,
    )


@pytest.fixture
def jira(cache, limiter, secrets, control_plane, fake_clock) -> JiraConnectorAdapter:
    return JiraConnectorAdapter(
        capabilities=JIRA_CAPABILITIES,
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
        capabilities=GITHUB_CAPABILITIES,
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


def build_catalog() -> SourceCatalog:
    """The two sources, built from the same capability dicts the adapters use."""
    return SourceCatalog(
        {
            ("github", "pull_requests"): Source(
                connector_type="github",
                resource="pull_requests",
                capabilities=CapabilityModel.from_dict(GITHUB_CAPABILITIES),
            ),
            ("jira", "issues"): Source(
                connector_type="jira",
                resource="issues",
                capabilities=CapabilityModel.from_dict(JIRA_CAPABILITIES),
            ),
        }
    )


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
    """
    return {"github": github, "jira": jira}
