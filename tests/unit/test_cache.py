"""The freshness cache — gate tests ``test_cache_hit`` and ``test_cross_tenant_isolation``."""

import pytest

from src.connectors.base import FetchRequest
from src.governance.cache import (
    CacheStatus,
    FreshnessCacheManager,
    build_key,
    normalize_request,
)

ACME = "tenant_acme"
GLOBEX = "tenant_globex"
SCOPE = "support"
CONNECTOR = "github"

REQUEST = FetchRequest(
    tenant_id=ACME,
    entitlement_scope=SCOPE,
    predicates={"repo": "ema/core", "state": "open"},
    projection=["title", "author"],
    limit=50,
)

ROWS = [{"title": "Fix login", "author": "alice"}]


@pytest.fixture
def cache(fake_redis, fake_clock):
    # An explicit TTL so the test does not depend on the deployed default.
    return FreshnessCacheManager(fake_redis, now_ms=fake_clock, ttl_ms=300_000)


# --- the key ---------------------------------------------------------------


def test_key_contains_tenant_and_scope_literally():
    """ADR-025, asserted on the key itself — not only on a miss.

    A test that merely checks two tenants miss each other passes against a key
    scheme that still leaks for some *other* pair of principals. The segments
    must be present and readable.
    """
    key = build_key(ACME, SCOPE, CONNECTOR, REQUEST)
    segments = key.split(":")
    assert segments[0] == "cache"
    assert segments[1] == ACME
    assert segments[2] == SCOPE
    assert segments[3] == CONNECTOR


@pytest.mark.parametrize(
    "tenant,scope", [("", SCOPE), (ACME, ""), ("", "")]
)
def test_empty_identity_segments_are_rejected(tenant, scope):
    """An empty string would produce a structurally valid but shared key."""
    with pytest.raises(ValueError, match="mandatory cache-key segment"):
        build_key(tenant, scope, CONNECTOR, REQUEST)


def test_identity_is_not_dissolved_into_the_hash():
    """Tenant and scope are separate segments, not hashed inputs.

    Keeping them readable means a leak is visible by inspecting a key rather
    than only by reversing a digest.
    """
    normalized = normalize_request(REQUEST)
    assert ACME not in normalized
    assert SCOPE not in normalized


def test_different_tenants_produce_different_keys():
    acme = build_key(ACME, SCOPE, CONNECTOR, REQUEST)
    globex = build_key(GLOBEX, SCOPE, CONNECTOR, REQUEST)
    assert acme != globex


def test_different_scopes_produce_different_keys():
    assert build_key(ACME, "support", CONNECTOR, REQUEST) != build_key(
        ACME, "admin", CONNECTOR, REQUEST
    )


def test_different_connectors_produce_different_keys():
    assert build_key(ACME, SCOPE, "github", REQUEST) != build_key(
        ACME, SCOPE, "jira", REQUEST
    )


def test_identical_requests_produce_identical_keys():
    assert build_key(ACME, SCOPE, CONNECTOR, REQUEST) == build_key(
        ACME, SCOPE, CONNECTOR, REQUEST
    )


def test_projection_order_does_not_split_the_cache():
    """Column order does not change which rows come back; stored rows are dicts."""
    a = FetchRequest(ACME, SCOPE, projection=["title", "author"])
    b = FetchRequest(ACME, SCOPE, projection=["author", "title"])
    assert build_key(ACME, SCOPE, CONNECTOR, a) == build_key(ACME, SCOPE, CONNECTOR, b)


def test_predicate_order_does_not_split_the_cache():
    a = FetchRequest(ACME, SCOPE, predicates={"repo": "ema/core", "state": "open"})
    b = FetchRequest(ACME, SCOPE, predicates={"state": "open", "repo": "ema/core"})
    assert build_key(ACME, SCOPE, CONNECTOR, a) == build_key(ACME, SCOPE, CONNECTOR, b)


def test_different_predicates_produce_different_keys():
    """The cache must not conflate two genuinely different questions."""
    a = FetchRequest(ACME, SCOPE, predicates={"state": "open"})
    b = FetchRequest(ACME, SCOPE, predicates={"state": "closed"})
    assert build_key(ACME, SCOPE, CONNECTOR, a) != build_key(ACME, SCOPE, CONNECTOR, b)


def test_different_pages_produce_different_keys():
    a = FetchRequest(ACME, SCOPE, page=None)
    b = FetchRequest(ACME, SCOPE, page="cursor-2")
    assert build_key(ACME, SCOPE, CONNECTOR, a) != build_key(ACME, SCOPE, CONNECTOR, b)


def test_different_limits_produce_different_keys():
    a = FetchRequest(ACME, SCOPE, limit=5)
    b = FetchRequest(ACME, SCOPE, limit=50)
    assert build_key(ACME, SCOPE, CONNECTOR, a) != build_key(ACME, SCOPE, CONNECTOR, b)


# --- GATE: test_cache_hit --------------------------------------------------


async def test_cache_hit(cache, fake_clock):
    """Written, then read back within the caller's staleness window."""
    key = cache.key(ACME, SCOPE, CONNECTOR, REQUEST)
    assert (await cache.get(key, max_staleness_ms=60_000)).status is CacheStatus.MISS

    await cache.set(key, ROWS, etag="etag-v1")

    lookup = await cache.get(key, max_staleness_ms=60_000)
    assert lookup.status is CacheStatus.HIT
    assert lookup.is_hit is True
    assert lookup.entry.rows == ROWS
    assert lookup.entry.etag == "etag-v1"
    assert lookup.age_ms == 0


async def test_entry_becomes_stale_for_a_stricter_caller(cache, fake_clock):
    """Staleness is judged per-read, so one entry serves callers differently."""
    key = cache.key(ACME, SCOPE, CONNECTOR, REQUEST)
    await cache.set(key, ROWS, etag="etag-v1")
    fake_clock.advance(30_000)

    assert (await cache.get(key, max_staleness_ms=60_000)).status is CacheStatus.HIT
    assert (await cache.get(key, max_staleness_ms=10_000)).status is CacheStatus.STALE
    # The strict caller still gets the entry back, so it can revalidate it.
    assert (await cache.get(key, max_staleness_ms=10_000)).entry.rows == ROWS


async def test_zero_staleness_demands_a_same_instant_entry(cache, fake_clock):
    """`max_staleness_ms=0` is the live-fetch end of the Phase 3 demo knob."""
    key = cache.key(ACME, SCOPE, CONNECTOR, REQUEST)
    await cache.set(key, ROWS)
    assert (await cache.get(key, max_staleness_ms=0)).status is CacheStatus.HIT
    fake_clock.advance(1)
    assert (await cache.get(key, max_staleness_ms=0)).status is CacheStatus.STALE


async def test_ttl_is_a_property_of_the_write_not_the_read(cache, fake_redis, fake_clock):
    """The central distinction of this module.

    A `max_staleness_ms=0` read must not cause a zero-TTL write — if it did, no
    later request could ever hit and the freshness demo would be unreproducible.
    """
    key = cache.key(ACME, SCOPE, CONNECTOR, REQUEST)
    await cache.get(key, max_staleness_ms=0)
    await cache.set(key, ROWS)
    ttl_ms = await fake_redis.pttl(key)
    assert ttl_ms > 0
    assert ttl_ms == pytest.approx(cache.ttl_ms, abs=50)


async def test_entry_disappears_after_the_server_ttl(cache, fake_redis):
    """Past TTL the entry is gone from Redis entirely — a MISS, not a STALE."""
    key = cache.key(ACME, SCOPE, CONNECTOR, REQUEST)
    await cache.set(key, ROWS)
    await fake_redis.delete(key)  # stand in for Redis expiry
    assert (await cache.get(key, max_staleness_ms=60_000)).status is CacheStatus.MISS


# --- the 304 path ----------------------------------------------------------


async def test_matching_etag_refreshes_fetched_at(cache, fake_clock):
    """`304` → the age resets, and no rows were transferred."""
    key = cache.key(ACME, SCOPE, CONNECTOR, REQUEST)
    await cache.set(key, ROWS, etag="etag-v1")
    fake_clock.advance(120_000)

    assert (await cache.get(key, max_staleness_ms=60_000)).status is CacheStatus.STALE

    revalidated = await cache.revalidate(key, source_etag="etag-v1")
    assert revalidated.status is CacheStatus.HIT
    assert revalidated.age_ms == 0
    assert revalidated.entry.rows == ROWS
    # And it is now fresh for a caller that just rejected it.
    assert (await cache.get(key, max_staleness_ms=60_000)).status is CacheStatus.HIT


async def test_changed_etag_forces_a_live_fetch(cache, fake_clock):
    key = cache.key(ACME, SCOPE, CONNECTOR, REQUEST)
    await cache.set(key, ROWS, etag="etag-v1")
    fake_clock.advance(120_000)
    assert (await cache.revalidate(key, source_etag="etag-v2")).status is CacheStatus.MISS


async def test_revalidating_an_absent_key_is_a_miss(cache):
    key = cache.key(ACME, SCOPE, CONNECTOR, REQUEST)
    assert (await cache.revalidate(key, source_etag="etag-v1")).status is CacheStatus.MISS


async def test_revalidation_without_an_etag_cannot_hit(cache):
    """No ETag means nothing to compare; it must not be treated as a match."""
    key = cache.key(ACME, SCOPE, CONNECTOR, REQUEST)
    await cache.set(key, ROWS, etag=None)
    assert (await cache.revalidate(key, source_etag=None)).status is CacheStatus.MISS


# --- GATE: test_cross_tenant_isolation -------------------------------------


async def test_cross_tenant_isolation(cache):
    """tenant_globex's entry for a byte-identical request is invisible to acme."""
    globex_request = FetchRequest(
        tenant_id=GLOBEX,
        entitlement_scope=SCOPE,
        predicates=dict(REQUEST.predicates),
        projection=list(REQUEST.projection),
        limit=REQUEST.limit,
    )
    # The request bodies are identical — only identity differs.
    assert normalize_request(globex_request) == normalize_request(REQUEST)

    globex_key = cache.key(GLOBEX, SCOPE, CONNECTOR, globex_request)
    await cache.set(globex_key, [{"title": "GLOBEX SECRET", "author": "mallory"}])

    acme_key = cache.key(ACME, SCOPE, CONNECTOR, REQUEST)
    assert acme_key != globex_key
    lookup = await cache.get(acme_key, max_staleness_ms=60_000)
    assert lookup.status is CacheStatus.MISS
    assert lookup.entry is None


async def test_cross_scope_isolation(cache):
    """The same tenant under a different entitlement scope must also miss.

    This is the half a tenant-only key scheme would get wrong: same tenant, two
    principals with different data entitlements.
    """
    support_key = cache.key(ACME, "support", CONNECTOR, REQUEST)
    admin_key = cache.key(ACME, "admin", CONNECTOR, REQUEST)
    await cache.set(admin_key, [{"title": "ADMIN ONLY", "author": "root"}])
    assert (await cache.get(support_key, max_staleness_ms=60_000)).status is CacheStatus.MISS


# --- failure handling ------------------------------------------------------


async def test_corrupt_entry_raises_rather_than_masquerading_as_a_miss(cache, fake_redis):
    """LAW 4.

    Swallowing a decode failure would turn a serialization bug into a permanent
    invisible cache bypass: every request would look like a cold start and spend
    a token forever, with nothing in the logs.
    """
    key = cache.key(ACME, SCOPE, CONNECTOR, REQUEST)
    await fake_redis.set(key, b"{not json")
    with pytest.raises(ValueError, match="corrupt cache entry"):
        await cache.get(key, max_staleness_ms=60_000)


async def test_invalidate_removes_the_entry(cache):
    key = cache.key(ACME, SCOPE, CONNECTOR, REQUEST)
    await cache.set(key, ROWS)
    await cache.invalidate(key)
    assert (await cache.get(key, max_staleness_ms=60_000)).status is CacheStatus.MISS
