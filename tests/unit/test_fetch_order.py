"""The ADR-024 invariant: cache before token, never token before cache.

**Why this is its own file.** Every other test in this phase asserts an
*outcome*, and an outcome cannot distinguish "cache hit, no token spent" from
"token spent, then refunded" — both leave `remaining` unchanged and both return
`served="cache"`. These tests assert the *order* of the steps, using a shared
timeline that the cache, the limiter and the secret store all append to.

Spending a token on a cache hit would break three things at once: the
"304 refreshes fetched_at without spending a token" guarantee, the determinism
of the Phase-3 rate-limit banner, and the Phase-4 load run — which would drain
tenant_acme's 5-token GitHub bucket on request 6 instead of serving from cache.
"""

import pytest

from src.connectors.base import FetchRequest
from src.connectors.errors import FailureMode
from src.governance.ratelimit import RateLimitPolicy
from src.models.errors import ApiError

ACME = "tenant_acme"
SCOPE = "support"
ACME_GITHUB = RateLimitPolicy(max_requests=5, window_sec=60, burst=2)


def gh_request(**kwargs) -> FetchRequest:
    predicates = {"repo": "ema/core", **kwargs.pop("predicates", {})}
    return FetchRequest(
        tenant_id=ACME, entitlement_scope=SCOPE, predicates=predicates, **kwargs
    )


# --- the order, on a live fetch --------------------------------------------


async def test_a_live_fetch_runs_the_steps_in_the_locked_order(github, timeline):
    """cache read -> consume token -> resolve secret."""
    await github.fetch(gh_request())
    assert timeline == ["read_cache", "consume_token", "resolve_secret"]


async def test_the_secret_is_resolved_after_the_token_is_spent(github, timeline):
    """Stated separately because it is the half ADR-024 does not cover.

    A credential resolved before the budget check would be decrypted for a
    request that is about to be refused — briefly holding plaintext for no reason.
    """
    await github.fetch(gh_request())
    assert timeline.index("consume_token") < timeline.index("resolve_secret")


async def test_a_miss_spends_exactly_one_token(github, limiter):
    await github.fetch(gh_request())
    assert limiter.consumed == [(ACME, "github")]


# --- GATE-adjacent: a cache hit spends nothing ------------------------------


async def test_a_cache_hit_never_calls_the_limiter(github, limiter, timeline):
    """The invariant, stated as directly as it can be."""
    await github.fetch(gh_request())
    assert limiter.consumed == [(ACME, "github")]

    timeline.clear()
    response = await github.fetch(gh_request())

    assert response.served == "cache"
    assert limiter.consumed == [(ACME, "github")]  # still just the one
    assert "consume_token" not in timeline


async def test_a_cache_hit_does_not_resolve_a_secret_either(github, timeline):
    """No downstream call means no credential needs decrypting."""
    await github.fetch(gh_request())
    timeline.clear()
    await github.fetch(gh_request())
    assert timeline == ["read_cache"]


async def test_the_cache_read_comes_first_even_on_a_hit(github, timeline):
    await github.fetch(gh_request())
    timeline.clear()
    await github.fetch(gh_request())
    assert timeline[0] == "read_cache"


async def test_repeated_hits_never_drain_the_bucket(github, limiter):
    """The Phase-4 property: 30k cached requests must not exhaust 7 tokens."""
    await github.fetch(gh_request())
    for _ in range(50):
        assert (await github.fetch(gh_request())).served == "cache"
    assert len(limiter.consumed) == 1
    assert await limiter.remaining(ACME, "github", ACME_GITHUB) == ACME_GITHUB.capacity - 1


# --- the 304 path also spends nothing ---------------------------------------


async def test_a_304_revalidation_spends_no_token(github, limiter, fake_clock):
    """Stale-but-present + matching ETag: fetched_at refreshes, budget does not move."""
    first = await github.fetch(gh_request(max_staleness_ms=60_000))
    assert first.served == "live"
    assert len(limiter.consumed) == 1

    fake_clock.advance(120_000)  # now stale for this caller, still inside TTL

    revalidated = await github.fetch(gh_request(max_staleness_ms=60_000))
    assert revalidated.served == "cache"
    assert revalidated.revalidated is True
    assert len(limiter.consumed) == 1, "a 304 must not spend a token"


async def test_a_304_refreshes_freshness(github, fake_clock):
    first = await github.fetch(gh_request(max_staleness_ms=60_000))
    fake_clock.advance(120_000)
    revalidated = await github.fetch(gh_request(max_staleness_ms=60_000))
    assert revalidated.fetched_at > first.fetched_at
    assert revalidated.fetched_at == pytest.approx(fake_clock() / 1000)


async def test_a_revalidated_entry_is_fresh_for_the_next_caller(github, fake_clock, limiter):
    """After the 304 the entry is young again, so the next read is a plain hit."""
    await github.fetch(gh_request(max_staleness_ms=60_000))
    fake_clock.advance(120_000)
    await github.fetch(gh_request(max_staleness_ms=60_000))

    plain_hit = await github.fetch(gh_request(max_staleness_ms=60_000))
    assert plain_hit.served == "cache"
    assert plain_hit.revalidated is False
    assert len(limiter.consumed) == 1


# --- staleness is a read property, not part of the identity of the question --


async def test_two_staleness_tolerances_share_one_cache_entry(github, limiter):
    """`max_staleness_ms` must not fragment the cache.

    If it were part of the key, a strict caller would write a second copy that a
    lenient caller could never find, and each tolerance would pay its own token.
    """
    await github.fetch(gh_request(max_staleness_ms=0))
    assert len(limiter.consumed) == 1

    lenient = await github.fetch(gh_request(max_staleness_ms=300_000))
    assert lenient.served == "cache"
    assert len(limiter.consumed) == 1


async def test_zero_staleness_still_writes_a_full_ttl_entry(github, cache, fake_redis):
    """TTL is a property of the write, staleness of the read.

    A `max_staleness_ms=0` request writing a zero-TTL entry would mean no later
    request could ever hit.
    """
    request = gh_request(max_staleness_ms=0)
    await github.fetch(request)
    key = cache.key(ACME, SCOPE, "github", request)
    assert await fake_redis.pttl(key) > 200_000


# --- different questions are not conflated ----------------------------------


async def test_a_different_predicate_is_a_different_question(github, limiter):
    await github.fetch(gh_request(predicates={"state": "open"}))
    await github.fetch(gh_request(predicates={"state": "closed"}))
    assert len(limiter.consumed) == 2


async def test_a_different_page_is_a_different_question(github, limiter):
    first = await github.fetch(gh_request(limit=5))
    await github.fetch(gh_request(limit=5, page=first.next_cursor))
    assert len(limiter.consumed) == 2


async def test_a_different_tenant_does_not_ride_anothers_cache(github, limiter):
    """Cross-tenant isolation, asserted through the adapter rather than the key."""
    await github.fetch(gh_request())
    assert len(limiter.consumed) == 1

    with pytest.raises(ApiError):
        # tenant_globex has no grant, so it is refused before it could ever
        # reach — let alone read — tenant_acme's cached entry.
        await github.fetch(
            FetchRequest(tenant_id="tenant_globex", entitlement_scope=SCOPE,
                         predicates={"repo": "ema/core"})
        )
    assert len(limiter.consumed) == 1


async def test_a_different_entitlement_scope_does_not_ride_the_cache(github, limiter):
    """Same tenant, different data entitlement — must be a separate entry."""
    await github.fetch(gh_request())
    assert len(limiter.consumed) == 1

    other = FetchRequest(
        tenant_id=ACME, entitlement_scope="admin", predicates={"repo": "ema/core"}
    )
    response = await github.fetch(other)
    assert response.served == "live"
    assert len(limiter.consumed) == 2


# --- failure paths spend nothing --------------------------------------------


async def test_a_throttled_fetch_resolves_no_secret(github, timeline, limiter):
    """Refused at the bucket: no credential should be decrypted."""
    for _ in range(ACME_GITHUB.capacity):
        await github.fetch(gh_request(predicates={"author": f"dev-{_}"}))
    timeline.clear()
    with pytest.raises(ApiError):
        await github.fetch(gh_request(predicates={"author": "one-too-many"}))
    assert "resolve_secret" not in timeline


async def test_a_forced_failure_short_circuits_before_the_cache(github, timeline):
    github.fail_next(FailureMode.TIMEOUT)
    with pytest.raises(ApiError):
        await github.fetch(gh_request())
    assert timeline == []


# --- the staleness knob must remain two-way ---------------------------------


async def test_zero_staleness_always_forces_a_live_fetch(github, limiter, fake_clock):
    """Regression: the knob was one-way.

    DoD §2 hard part 4 demonstrates freshness by moving `max_staleness_ms`
    between 0 and 60000 and watching `served` flip. The adapter used to
    revalidate every stale entry unconditionally — and because the mock dataset
    is a module-level constant, the candidate ETag ALWAYS matched, so every
    conditional request succeeded. Once an entry existed, no value of
    `max_staleness_ms` could produce `served="live"` again and the demo showed
    cache -> cache.
    """
    assert (await github.fetch(gh_request(max_staleness_ms=0))).served == "live"

    fake_clock.advance(1)
    assert (await github.fetch(gh_request(max_staleness_ms=0))).served == "live"

    fake_clock.advance(120_000)
    assert (await github.fetch(gh_request(max_staleness_ms=0))).served == "live"

    # Three live fetches means three tokens — a live fetch is not free.
    assert len(limiter.consumed) == 3


async def test_the_staleness_knob_flips_served_both_ways(github, fake_clock):
    """The demo itself: same request, two tolerances, two outcomes."""
    await github.fetch(gh_request(max_staleness_ms=60_000))
    fake_clock.advance(1_000)

    assert (await github.fetch(gh_request(max_staleness_ms=60_000))).served == "cache"
    assert (await github.fetch(gh_request(max_staleness_ms=0))).served == "live"
    assert (await github.fetch(gh_request(max_staleness_ms=60_000))).served == "cache"


async def test_a_lenient_caller_still_revalidates_rather_than_refetching(
    github, limiter, fake_clock
):
    """The 304 path survives the fix — it applies to tolerant callers only."""
    await github.fetch(gh_request(max_staleness_ms=60_000))
    assert len(limiter.consumed) == 1

    fake_clock.advance(120_000)
    revalidated = await github.fetch(gh_request(max_staleness_ms=60_000))
    assert revalidated.served == "cache"
    assert revalidated.revalidated is True
    assert len(limiter.consumed) == 1, "a 304 must still spend no token"
