"""Per-tenant token bucket, refilled and consumed atomically in Redis.

**One key per ``(tenant, connector)``** — ``ratelimit:{tenant_id}:{connector_type}``
— matching the primary key of ``rate_limit_policies`` exactly, so every bucket's
capacity comes from a seeded row. A per-*user* bucket was considered and
rejected: the schema has no per-user budget row, so that bucket could only ever
be handed an invented limit, and a bucket whose capacity is fabricated is a
constant rather than a fairness mechanism. Per-user fairness inside a tenant
needs a fair scheduler, which is described but not built.

**Refill and consume are one Lua script**, so a concurrent caller cannot read a
token count between another caller's refill and its decrement. ``remaining``
comes back from that *same* call rather than a follow-up read: a second round
trip is not atomic with the first, so the ``/metrics`` gauge and the envelope's
``rate_limit_status`` could otherwise report a figure that was never true.

The script is also the *only* place the bucket arithmetic exists. A Python
mirror of it for unit tests would be two implementations of the same formula,
where the test passes while production is wrong — so the tests run this script,
against ``fakeredis[lua]``.
"""

import math
from dataclasses import dataclass
from typing import Any

from src.governance.clock import NowMs, wall_clock_ms
from src.models.errors import ApiError, ErrorCode

#: Refill + consume, atomic. ``amount=0`` is a peek: it refills and reports
#: without spending, so callers never need a second, non-atomic read.
#:
#: Tokens are stored as a float so partial refill is not lost to rounding —
#: truncating each refill to an integer would leak budget to any caller polling
#: faster than one token per interval.
CONSUME_LUA = """
local key       = KEYS[1]
local capacity  = tonumber(ARGV[1])
local refill_ms = tonumber(ARGV[2])
local now_ms    = tonumber(ARGV[3])
local ttl_ms    = tonumber(ARGV[4])
local amount    = tonumber(ARGV[5])

local state  = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts     = tonumber(state[2])
if tokens == nil or ts == nil then
  tokens = capacity
  ts = now_ms
end

-- max(0, ...) so a clock that steps backwards cannot mint tokens.
local elapsed = math.max(0, now_ms - ts)
tokens = math.min(capacity, tokens + (elapsed / refill_ms))

local allowed = 0
local retry_after_ms = 0
if tokens >= amount then
  tokens = tokens - amount
  allowed = 1
else
  retry_after_ms = math.ceil((amount - tokens) * refill_ms)
end

-- max(ts, now_ms), NOT now_ms: if the wall clock stepped BACKWARDS the guard
-- above correctly refuses to refill, but writing the moved-back instant would
-- mean that when the clock recovers the bucket sees the whole jump as elapsed
-- time and refills for free. The bucket's clock must be monotonic even when
-- the system's is not.
redis.call('HMSET', key, 'tokens', tokens, 'ts', math.max(ts, now_ms))
redis.call('PEXPIRE', key, ttl_ms)
return {allowed, math.floor(tokens), retry_after_ms}
"""

KEY_PREFIX = "ratelimit"


@dataclass(frozen=True)
class RateLimitPolicy:
    """A budget, as stored in ``rate_limit_policies``."""

    max_requests: int
    window_sec: int
    burst: int = 0

    def __post_init__(self) -> None:
        if self.max_requests <= 0:
            raise ValueError(f"max_requests must be positive (got {self.max_requests})")
        if self.window_sec <= 0:
            raise ValueError(f"window_sec must be positive (got {self.window_sec})")
        if self.burst < 0:
            raise ValueError(f"burst cannot be negative (got {self.burst})")

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "RateLimitPolicy":
        """Build from a ``ControlPlaneRepository.get_rate_limit_policy`` row."""
        return cls(
            max_requests=int(row["max_requests"]),
            window_sec=int(row["window_sec"]),
            burst=int(row.get("burst") or 0),
        )

    @property
    def capacity(self) -> int:
        """Tokens a cold bucket holds: the steady rate **plus** the burst allowance.

        This is what makes ``burst`` a behaviour rather than a column — a cold
        bucket admits ``max_requests + burst`` back-to-back before throttling.
        """
        return self.max_requests + self.burst

    @property
    def refill_ms(self) -> float:
        """Milliseconds to earn one token, at the steady rate.

        Derived from ``max_requests`` alone, not from ``capacity``: the burst is
        a one-off allowance on top of the sustained rate, so letting it speed up
        refill would raise the long-run rate the policy is there to cap.
        """
        return (self.window_sec * 1000) / self.max_requests

    @property
    def key_ttl_ms(self) -> int:
        """How long an idle bucket's state is kept.

        Long enough to refill from empty to full — evicting sooner would reset a
        drained bucket to full early and hand back budget that was never earned.
        """
        return int(math.ceil(self.capacity * self.refill_ms)) + self.window_sec * 1000


@dataclass(frozen=True)
class RateLimitDecision:
    """The outcome of one bucket interaction."""

    allowed: bool
    remaining: int
    retry_after_ms: int


class TokenBucketRateLimiter:
    """Atomic per-tenant token bucket over Redis.

    Takes an already-resolved ``(tenant_id, connector_type)`` and imports
    nothing from ``src.gateway`` — it never inspects a request. It is called
    from inside ``adapter.fetch()``, *after* the cache is consulted, never as a
    route dependency: a route-level limiter would spend a token on a cache hit,
    which makes no downstream call and therefore consumes none of the downstream
    budget this bucket models.
    """

    def __init__(self, redis: Any, now_ms: NowMs = wall_clock_ms) -> None:
        self._redis = redis
        self._now_ms = now_ms
        self._script = redis.register_script(CONSUME_LUA)

    @staticmethod
    def key(tenant_id: str, connector_type: str) -> str:
        return f"{KEY_PREFIX}:{tenant_id}:{connector_type}"

    async def _run(
        self, tenant_id: str, connector_type: str, policy: RateLimitPolicy, amount: int
    ) -> RateLimitDecision:
        allowed, remaining, retry_after_ms = await self._script(
            keys=[self.key(tenant_id, connector_type)],
            args=[
                policy.capacity,
                policy.refill_ms,
                self._now_ms(),
                policy.key_ttl_ms,
                amount,
            ],
        )
        return RateLimitDecision(
            allowed=bool(allowed),
            remaining=int(remaining),
            retry_after_ms=int(retry_after_ms),
        )

    async def try_consume(
        self, tenant_id: str, connector_type: str, policy: RateLimitPolicy
    ) -> RateLimitDecision:
        """Spend one token if there is one. Never raises on an empty bucket."""
        return await self._run(tenant_id, connector_type, policy, amount=1)

    async def consume(
        self, tenant_id: str, connector_type: str, policy: RateLimitPolicy
    ) -> RateLimitDecision:
        """Spend one token, or raise the caller-facing 429.

        ``ApiError`` fills in the async-reroute ``suggested_action`` for
        ``RATE_LIMIT_EXHAUSTED`` itself, so the friendly error names the way out
        no matter which connector raised it.
        """
        decision = await self.try_consume(tenant_id, connector_type, policy)
        if not decision.allowed:
            raise ApiError(
                code=ErrorCode.RATE_LIMIT_EXHAUSTED,
                http=429,
                message=(
                    f"Rate limit exhausted for tenant {tenant_id!r} on connector "
                    f"{connector_type!r} ({policy.max_requests} requests per "
                    f"{policy.window_sec}s, burst {policy.burst})."
                ),
                retry_after_ms=decision.retry_after_ms,
            )
        return decision

    async def remaining(self, tenant_id: str, connector_type: str, policy: RateLimitPolicy) -> int:
        """Tokens left, refilled to *now*, without spending one.

        Feeds the envelope's ``rate_limit_status`` and the
        ``rate_limit_remaining`` gauge. Uses the same script as
        :meth:`consume` with ``amount=0``, so the refill arithmetic has exactly
        one implementation.
        """
        decision = await self._run(tenant_id, connector_type, policy, amount=0)
        return decision.remaining
