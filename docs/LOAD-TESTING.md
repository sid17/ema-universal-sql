# How to Run the Load Test

> The numbers are in **[LOAD-RESULTS.md](LOAD-RESULTS.md)**. Raw k6 summaries: `docs/artifacts/load/`.

## Setup

```bash
make up          # app (8 workers), postgres, redis
make seed        # control plane
make load-seed   # 20 synthetic load tenants
```

## Running it

```bash
make load-mt                                       # S3 — realistic mix, ~95% cache hit
make load-mt MISS_RATE=0                           # S2 — 100% hit, the engine alone
make load-mt MISS_RATE=1.0 LOAD_MAX_REQUESTS=84    # S4 — cold cache, GitHub's real quota
make load-mt RATE=750                              # any other offered rate
```

`make load-mt` rebuilds the image, disables span export (it distorts what is being measured), seeds
the tenants, flushes the cache so every run starts from a known state, runs k6 in its own container,
and restores the defaults afterwards. It verifies the app actually received the configuration before
measuring anything. The k6 script has no remote imports, so it works offline.

## The scenarios

| | What it is | Isolates |
|---|---|---|
| **S2** | 2-source join, 100% cache hit | the engine, with the connectors removed |
| **S3** | 2-source join, ~95% hit, 20 tenants | production shape |
| **S4** | 2-source join, 0% hit, real quota | the connector and the rate limiter |

`LOAD_MAX_REQUESTS=84` is what makes S4 mean anything. Load tenants are seeded with a deliberately
generous budget so S2 and S3 measure the *engine* rather than the token bucket; `84` is GitHub's real
5,000/hour. Burst is derived as one tenth of the budget — a fixed burst large enough to cover the
whole quota lets every call through and the ceiling never appears.

## Method

Open model (`constant-arrival-rate`): the offered rate is held constant, the **achieved** rate is
reported next to it, and `dropped_iterations` is always printed. Thresholds are never tuned to pass.
A 10s warm-up precedes each scenario and is excluded from the numbers.

Every synthetic row carries its tenant's id in a *projected* column, and k6 checks every row of every
response for a foreign stamp — so tenant isolation is asserted under concurrency rather than only in
a unit test.

## What this deliberately does not do

- **One load generator, one host.** The honest ceiling is one laptop's.
- **Mocked connectors.** Latency and quota are simulated faithfully; a real API's tail behaviour,
  throttling headers and pagination cost at scale are not.
- **60s, not a soak.** Memory growth, cache eviction under pressure and pool exhaustion are untested.
- **Fixed 8 workers.** No autoscaling.
