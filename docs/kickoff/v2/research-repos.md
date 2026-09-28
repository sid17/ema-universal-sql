# GitHub Research — Phase 1 (connectors + governance)

> **Scope of this pass.** `03-BUILD-PROCESS.md` step 1 caps per-phase research at a 10–20 minute
> confirmation and forbids it from reopening a locked decision. Phase 1's connector patterns were
> already settled by **Card 2 (`airbyte-python-cdk`)** in
> `research/prototype-prior-art.md` — `RequestOption`,
> pagination as *strategy ⊕ placement*, error mapping as a match→action table. Those are not re-surveyed.
>
> **One genuine open question remained:** the shape of the Redis token bucket. The brief names
> *"token bucket with burst"* explicitly (line 158), and no card covered it. That is all this pass looked at.

## Search keywords

`redis lua token bucket` · `python redis ratelimit` · `limits redis` — plus three named Python repos checked
directly, since the keyword results were dominated by sub-10-star hobby projects with no community validation.

## Repos analyzed

### `alisaifee/limits` (⭐ 647, Python, updated 2026-09-28)
- **What it does:** the storage/strategy layer underneath Flask-Limiter. Redis, Memcached, MongoDB backends.
- **The finding that matters — it has no token bucket.** Its Lua scripts are
  `acquire_moving_window.lua`, `acquire_sliding_window.lua`, `incr_expire.lua`. The most-depended-on rate
  limiter in the Python ecosystem deliberately implements **window counters, not buckets**.
- **Consequence for us:** there is no drop-in Python token-bucket Lua to copy. We write ours. What we copy is
  the *idiom*, which is consistent across both scripts: all state in `KEYS`, all parameters in `ARGV`,
  millisecond units, `PX` expiry set inside the script, one round trip returning the decision.
- **Trade-off they took:** a moving window is exact over its window but cannot express burst-on-top-of-rate,
  which is the property the brief asks us to demonstrate. Their choice is right for a general library
  (no `burst` parameter to explain) and wrong for us.
- **Confidence:** High — read both Lua scripts in full.

### `vutran1710/PyrateLimiter` (⭐ 525, Python, updated 2026-09-23)
- **What it does:** the leaky/token-bucket family, with pluggable bucket backends. It is what
  `fastapi-limiter` now delegates to.
- **Key pattern — an injected `AbstractClock`, not `time.time()` at the call site.** `clocks.py` defines
  `MonotonicClock`, `WallClock`, `PostgresClock`, `MonotonicAsyncClock`, and documents *why* the distinction
  is load-bearing: *"Needed where timestamps are compared across processes or hosts — a monotonic clock is
  only meaningful within one machine's boot. Use it for any bucket whose state is shared through Redis."*
- **Relevance to us:** answers the open question twice over. See "Patterns to adopt" below.
- **Confidence:** High — read `clocks.py` in full, plus the bucket/limiter module layout.

### `long2ice/fastapi-limiter` (⭐ 788, Python, updated 2026-09-20)
- **What it does:** FastAPI dependency-injection wrapper for rate limiting.
- **Finding:** it **deleted its own Lua script** and now wraps `PyrateLimiter`. What remains is a thin
  `__call__(request, response)` dependency that resolves an identifier, calls `try_acquire_async`, and hands
  failure to a `callback`.
- **Relevance:** confirms the layering, and confirms a boundary. The limiter is called with an
  already-resolved key — it never inspects the request itself. Our limiter takes
  `(tenant_id, connector_type)` and knows nothing about HTTP. But we deliberately **do not** adopt its
  route-dependency placement: our bucket is consumed **inside `adapter.fetch()`, after the cache check**, not
  at the route. A route-level limiter would spend a token on a cache hit, which the phase file forbids in
  bold ("cache before token, never token before cache").
- **Confidence:** Medium — read the public dependency surface, not the internals.

## Comparison

| Dimension | `limits` | `PyrateLimiter` | `fastapi-limiter` |
|---|---|---|---|
| Algorithm | moving / sliding window | **leaky + token bucket** | delegates |
| Burst expressible | no | yes | via delegate |
| Redis Lua | yes, two scripts | via bucket backend | removed |
| Time source | passed in as `ARGV` | **injected clock object** | delegate's |
| What we take | Lua calling idiom | the clock abstraction + where the limiter sits in the stack | the "limiter never sees the request" boundary |

## Patterns to adopt

- **Inject the clock; do not call `time.time()` inside the limiter.** From `PyrateLimiter`'s `AbstractClock`.
  This is the finding that changes Phase 1's design, because it decides whether an acceptance test can pass
  at all. `test_bucket_burst` has to assert *"after `window_sec/max_requests` of refill, exactly one more
  request is admitted"* — with a hard-coded clock that test must **really sleep** for the refill interval
  (12s at `max_requests=5, window_sec=60`), which is too slow for the `pytest -q tests/unit` commit hook.
  With an injected clock it is an in-memory assertion. The production default is a **wall clock in epoch
  milliseconds**, for PyrateLimiter's stated reason: bucket state lives in Redis and is therefore compared
  across processes, where `monotonic()` is meaningless.
- **All bucket state in `KEYS`, all parameters in `ARGV`, milliseconds throughout, one round trip returning
  the decision.** From `limits`. Our script returns `{allowed, remaining, retry_after_ms}` rather than a bare
  boolean, because the envelope's `rate_limit_status` and the `/metrics` gauge both need `remaining` and the
  `429` needs `Retry-After` — fetching those in a second call would not be atomic with the consume.
- **The limiter receives a resolved key and never inspects a request.** From `fastapi-limiter`'s boundary,
  minus its placement.

## Patterns to skip

- **Window counters** (`limits`) — correct for a general library, but cannot express burst, which the brief
  names explicitly (line 158).
- **Route-level limiter dependency** (`fastapi-limiter`) — would consume a token before the cache is
  consulted, inverting the order the phase file makes load-bearing.
- **Pluggable multi-backend bucket storage** (`PyrateLimiter`) — Redis is locked; an abstract bucket backend
  would be a speculative abstraction with exactly one implementation (LAW 5).
- **Re-surveying the airbyte CDK.** Card 2 settled `RequestOption`, pagination strategy ⊕ placement, and the
  error action enum. Research may inform how we implement a locked decision, not reopen it.

## Verdict

One decision changed by this pass: **`TokenBucketRateLimiter` takes a clock**, defaulting to wall-clock epoch
ms, because the phase's own burst acceptance test is otherwise a 12-second sleep in the unit suite. Everything
else Phase 1 needs was already settled by Card 2.

→ Step 2: `/architecture-extraction` → `docs/kickoff/v2/architecture.md`
