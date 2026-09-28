"""The token bucket — gate tests ``test_bucket_drain`` and ``test_bucket_burst``.

These run the real Lua script against `fakeredis`, not a Python reimplementation
of the same arithmetic. Two implementations is how a unit test goes green while
production is wrong (ADR-019).
"""

import pytest

from src.governance.ratelimit import (
    RateLimitPolicy,
    TokenBucketRateLimiter,
)
from src.models.errors import ASYNC_REROUTE_ACTION, ApiError, ErrorCode

# tenant_acme / github, as seeded: deliberately tiny so the 429 demo drains fast.
ACME_GITHUB = RateLimitPolicy(max_requests=5, window_sec=60, burst=2)

TENANT = "tenant_acme"
CONNECTOR = "github"


@pytest.fixture
def limiter(fake_redis, fake_clock):
    return TokenBucketRateLimiter(fake_redis, now_ms=fake_clock)


# --- the policy arithmetic -------------------------------------------------


def test_capacity_is_rate_plus_burst():
    """What makes `burst` a behaviour rather than a column in a table."""
    assert ACME_GITHUB.capacity == 7


def test_refill_rate_ignores_burst():
    """Burst is a one-off allowance, not a speed-up of the sustained rate.

    If burst fed into refill, the long-run rate would exceed the cap the policy
    exists to enforce.
    """
    assert ACME_GITHUB.refill_ms == 12_000  # 60s / 5 requests
    assert RateLimitPolicy(max_requests=5, window_sec=60, burst=500).refill_ms == 12_000


def test_key_ttl_outlives_a_full_refill():
    """Evicting early would reset a drained bucket to full ahead of schedule."""
    assert ACME_GITHUB.key_ttl_ms >= ACME_GITHUB.capacity * ACME_GITHUB.refill_ms


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_requests": 0, "window_sec": 60},
        {"max_requests": -1, "window_sec": 60},
        {"max_requests": 5, "window_sec": 0},
        {"max_requests": 5, "window_sec": 60, "burst": -1},
    ],
)
def test_nonsense_policies_are_rejected_at_construction(kwargs):
    """`max_requests=0` would divide by zero inside the refill calculation."""
    with pytest.raises(ValueError):
        RateLimitPolicy(**kwargs)


def test_policy_reads_a_control_plane_row():
    policy = RateLimitPolicy.from_row(
        {
            "tenant_id": TENANT,
            "connector_type": "github",
            "max_requests": 5,
            "window_sec": 60,
            "burst": 2,
        }
    )
    assert policy == ACME_GITHUB


def test_policy_tolerates_a_null_burst_column():
    """`burst` is nullable in the schema; NULL means no burst, not a crash."""
    assert RateLimitPolicy.from_row({"max_requests": 5, "window_sec": 60, "burst": None}).burst == 0


# --- the key ---------------------------------------------------------------


def test_key_is_tenant_and_connector_only():
    """ADR-020: one bucket per (tenant, connector), matching the policy PK."""
    assert TokenBucketRateLimiter.key(TENANT, CONNECTOR) == "ratelimit:tenant_acme:github"


async def test_buckets_are_isolated_per_tenant(limiter):
    """tenant_load draining must not affect tenant_acme."""
    for _ in range(ACME_GITHUB.capacity):
        await limiter.consume("tenant_load", CONNECTOR, ACME_GITHUB)
    assert await limiter.remaining("tenant_load", CONNECTOR, ACME_GITHUB) == 0
    assert await limiter.remaining(TENANT, CONNECTOR, ACME_GITHUB) == ACME_GITHUB.capacity


async def test_buckets_are_isolated_per_connector(limiter):
    """Draining GitHub must leave the same tenant's Jira budget untouched."""
    for _ in range(ACME_GITHUB.capacity):
        await limiter.consume(TENANT, "github", ACME_GITHUB)
    assert await limiter.remaining(TENANT, "github", ACME_GITHUB) == 0
    assert await limiter.remaining(TENANT, "jira", ACME_GITHUB) == ACME_GITHUB.capacity


# --- GATE: test_bucket_drain ----------------------------------------------


async def test_bucket_drain(limiter):
    """Consume past the budget → RATE_LIMIT_EXHAUSTED, positive retry_after_ms,
    and `remaining()` reads 0."""
    for _ in range(ACME_GITHUB.capacity):
        decision = await limiter.consume(TENANT, CONNECTOR, ACME_GITHUB)
        assert decision.allowed is True

    assert await limiter.remaining(TENANT, CONNECTOR, ACME_GITHUB) == 0

    with pytest.raises(ApiError) as raised:
        await limiter.consume(TENANT, CONNECTOR, ACME_GITHUB)

    error = raised.value
    assert error.code is ErrorCode.RATE_LIMIT_EXHAUSTED
    assert error.http == 429
    assert error.retry_after_ms > 0


async def test_drain_error_names_the_async_reroute(limiter):
    """Brief lines 110/158: the friendly error must name the way out."""
    for _ in range(ACME_GITHUB.capacity):
        await limiter.consume(TENANT, CONNECTOR, ACME_GITHUB)
    with pytest.raises(ApiError) as raised:
        await limiter.consume(TENANT, CONNECTOR, ACME_GITHUB)
    assert raised.value.suggested_action == ASYNC_REROUTE_ACTION
    assert "/v1/query/async" in raised.value.suggested_action


async def test_retry_after_is_one_refill_interval_when_empty(limiter):
    """An exhausted bucket should say exactly how long until one token exists."""
    for _ in range(ACME_GITHUB.capacity):
        await limiter.consume(TENANT, CONNECTOR, ACME_GITHUB)
    with pytest.raises(ApiError) as raised:
        await limiter.consume(TENANT, CONNECTOR, ACME_GITHUB)
    assert raised.value.retry_after_ms == int(ACME_GITHUB.refill_ms)


async def test_try_consume_does_not_raise_when_empty(limiter):
    """The non-raising variant, for callers that report rather than fail."""
    for _ in range(ACME_GITHUB.capacity):
        await limiter.try_consume(TENANT, CONNECTOR, ACME_GITHUB)
    decision = await limiter.try_consume(TENANT, CONNECTOR, ACME_GITHUB)
    assert decision.allowed is False
    assert decision.remaining == 0
    assert decision.retry_after_ms > 0


# --- GATE: test_bucket_burst ----------------------------------------------


async def test_bucket_burst(limiter, fake_clock):
    """The brief names burst explicitly (line 158), so it gets its own assertion.

    A cold bucket admits `max_requests + burst` back-to-back; the next request
    throttles; after exactly one refill interval, exactly ONE more is admitted.
    """
    # 1. Cold bucket admits 5 + 2 = 7 back-to-back, with no time passing.
    for i in range(ACME_GITHUB.capacity):
        decision = await limiter.consume(TENANT, CONNECTOR, ACME_GITHUB)
        assert decision.allowed is True, f"request {i + 1} should have been admitted"

    # 2. The 8th throttles.
    with pytest.raises(ApiError):
        await limiter.consume(TENANT, CONNECTOR, ACME_GITHUB)

    # 3. Exactly one refill interval passes.
    fake_clock.advance(int(ACME_GITHUB.refill_ms))

    # 4. Exactly one more is admitted...
    decision = await limiter.consume(TENANT, CONNECTOR, ACME_GITHUB)
    assert decision.allowed is True

    # 5. ...and only one.
    with pytest.raises(ApiError):
        await limiter.consume(TENANT, CONNECTOR, ACME_GITHUB)


async def test_burst_is_spent_before_the_steady_rate(limiter, fake_clock):
    """With burst=0 the same policy admits only `max_requests`.

    Isolates the burst allowance: same rate, same window, different capacity.
    """
    no_burst = RateLimitPolicy(max_requests=5, window_sec=60, burst=0)
    for _ in range(5):
        assert (await limiter.consume(TENANT, CONNECTOR, no_burst)).allowed is True
    with pytest.raises(ApiError):
        await limiter.consume(TENANT, CONNECTOR, no_burst)


async def test_refill_is_gradual_not_a_window_reset(limiter, fake_clock):
    """A token bucket, not a fixed window.

    After half a window the bucket must hold roughly half the rate — a
    fixed-window counter would still be empty, then jump to full at the boundary.
    """
    for _ in range(ACME_GITHUB.capacity):
        await limiter.consume(TENANT, CONNECTOR, ACME_GITHUB)
    fake_clock.advance(30_000)  # half of a 60s window
    assert await limiter.remaining(TENANT, CONNECTOR, ACME_GITHUB) == 2


async def test_refill_is_capped_at_capacity(limiter, fake_clock):
    """An idle bucket must not accumulate beyond its ceiling."""
    await limiter.consume(TENANT, CONNECTOR, ACME_GITHUB)
    fake_clock.advance(10 * ACME_GITHUB.window_sec * 1000)
    assert await limiter.remaining(TENANT, CONNECTOR, ACME_GITHUB) == ACME_GITHUB.capacity


async def test_partial_refill_is_not_lost_to_rounding(limiter, fake_clock):
    """Tokens are stored as a float.

    Truncating each refill to an integer would leak budget to a caller polling
    faster than one token per interval: twelve 1000ms steps must earn exactly
    one token, not zero.
    """
    for _ in range(ACME_GITHUB.capacity):
        await limiter.consume(TENANT, CONNECTOR, ACME_GITHUB)
    for _ in range(12):
        fake_clock.advance(1_000)
        await limiter.remaining(TENANT, CONNECTOR, ACME_GITHUB)
    assert (await limiter.consume(TENANT, CONNECTOR, ACME_GITHUB)).allowed is True


async def test_clock_running_backwards_cannot_mint_tokens(limiter, fake_clock):
    """Wall clocks can step backwards (NTP). That must not be free budget.

    The recovery half is the half that matters. Clamping `elapsed` to >= 0 stops
    the jump itself from refilling, but if the bucket also records the
    moved-back instant, the NEXT call after the clock recovers sees the whole
    jump as elapsed time and refills for free. This test failed at the recovery
    step until the script was changed to persist `max(ts, now_ms)`.
    """
    for _ in range(ACME_GITHUB.capacity):
        await limiter.consume(TENANT, CONNECTOR, ACME_GITHUB)
    assert await limiter.remaining(TENANT, CONNECTOR, ACME_GITHUB) == 0

    # An NTP correction steps the clock back. `advance()` is bypassed on purpose:
    # it refuses to go backwards, and the system clock does not.
    fake_clock._now_ms -= 60_000
    assert await limiter.remaining(TENANT, CONNECTOR, ACME_GITHUB) == 0

    # ...and the clock recovers to where it already was. No real time has
    # passed, so no budget may have been earned.
    fake_clock._now_ms += 60_000
    assert await limiter.remaining(TENANT, CONNECTOR, ACME_GITHUB) == 0

    with pytest.raises(ApiError):
        await limiter.consume(TENANT, CONNECTOR, ACME_GITHUB)


# --- remaining() -----------------------------------------------------------


async def test_remaining_does_not_spend_a_token(limiter):
    """It feeds a gauge and the envelope; reporting must not cost budget."""
    before = await limiter.remaining(TENANT, CONNECTOR, ACME_GITHUB)
    for _ in range(3):
        await limiter.remaining(TENANT, CONNECTOR, ACME_GITHUB)
    assert await limiter.remaining(TENANT, CONNECTOR, ACME_GITHUB) == before == 7


async def test_remaining_tracks_consumption(limiter):
    await limiter.consume(TENANT, CONNECTOR, ACME_GITHUB)
    assert await limiter.remaining(TENANT, CONNECTOR, ACME_GITHUB) == 6
