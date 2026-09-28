# Load Test Results

> Method: **[LOAD-TESTING.md](LOAD-TESTING.md)** · What's left: **[LOAD-NEXT-STEPS.md](LOAD-NEXT-STEPS.md)**
> Artifacts: `docs/artifacts/load/`.
>
> **Status: in progress.** S2, S3 and S4 are built and run. S1 (single source) is specified but not
> built. The throughput answer below has an **unresolved reproducibility problem** that is stated
> rather than smoothed over.

---

## 1. Verdict

| Question | Answer |
|---|---|
| What does a warm two-source entitled join cost? | **24.8ms p50** at 200 QPS — 20× inside the 500ms budget. |
| Does the engine hold 500 QPS? | **Not reproducibly.** One run did; five did not. Sustained ceiling measures **~400 req/s**. |
| Is the ceiling CPU? | **No.** App CPU peaks at **220% of 1200%** available. Postgres 9%, Redis 2%. |
| Does tenant isolation hold under load? | **Yes.** Zero foreign rows across >100,000 requests. |
| Does the cache behave as designed? | **Yes.** Measured hit ratio 93.5–95.0% against an intended 95%. |
| Does it degrade honestly when saturated? | **Yes.** 306–382 partial results, correctly labelled; no wrong answers. |
| Does the rate limiter bind at a real quota? | **Yes.** 761 × 429 at 0% hit ratio; served requests still inside SLO. |

---

## 2. S2 and S4 — the two bounds

Both at 200 QPS for 15s, 8 workers, 20 tenants.

| | Offered | Achieved | p50 | p95 | Hit ratio | 429s |
|---|---|---|---|---|---|---|
| **S2** — 100% hit, join only | 200 | 200.0 | **24.8ms** | 164.7ms | 100.00% | 0 |
| **S4** — 0% hit, GitHub's real quota | 200 | 200.1 | 196.7ms | 378.8ms | 0.00% | **761** |

**S2 is the engine's floor: 24.8ms p50 for a two-source entitled join, cache-warm.** That is the
number to compare everything else against — 20× inside the 500ms P50 budget.

**S4 behaves exactly as designed.** With the connector budget seeded at GitHub's real 5,000/hour
(1.39 req/s), a 0% hit ratio throttles **761 of 3,000 requests** — and the requests that *are*
served stay well inside the SLO (p50 197ms, p95 379ms), with zero failed checks. The system refuses
work it cannot do rather than collapsing, which is the whole point of the scenario.

This only worked after two fixes, both worth recording:

- Load tenants ship a generous budget (5,000/60s) so S1–S3 measure the engine. S4 needs the *real*
  quota — `LOAD_MAX_REQUESTS=84`.
- Burst was a fixed 500. On an 84-request budget that means capacity 584, so the first 584 calls
  sail through and the quota never binds. **S4 reported 0 × 429 and looked like a pass.** Burst is
  now derived as one tenth of the budget.

---

## 3. S3 at 500 QPS — what we ran

All runs: 8 workers, 20 tenants, 200 rows/source, 5% intended miss rate, 60s steady state after a
warm-up that is excluded.

| Offered | Achieved | Dropped | p50 | p95 | Hit ratio | Leaks |
|---|---|---|---|---|---|---|
| 500 *(run 1)* | **500.0** | 0 | **141ms** | **447ms** | 94.94% | 0 |
| 500 *(run 2)* | 400.7 | 5,959 | 4,720ms | 6,064ms | 93.69% | 0 |
| 500 *(run 3)* | 409.7 | 5,420 | 4,439ms | 5,932ms | 94.01% | 0 |
| 500 *(run 4)* | 408.2 | 5,507 | 3,972ms | 6,214ms | 94.11% | 0 |
| 500 *(run 5)* | 427.3 | 4,362 | 3,803ms | 5,563ms | 93.78% | 0 |
| 550 | 398.4 | 9,097 | 5,050ms | 6,199ms | 93.47% | 0 |
| 600 | 395.5 | 12,269 | 5,180ms | 6,499ms | 93.58% | 0 |
| 750 | 385.9 | 21,852 | 5,524ms | 7,024ms | 94.02% | 0 |

**Run 1 is the outlier, not the rule.** It was the first 500 QPS run and cleared the SLO
comfortably — p50 141ms against a 500ms budget, p95 447ms against 1.5s, zero dropped iterations,
90,403 checks passed. Four attempts to reproduce it since have landed at 400–427 req/s with p50 around
4 seconds. We have not root-caused the difference and are not reporting run 1 as the result.

Things ruled out so far:

- **Audit table growth** — truncated `audit_logs` (168,617 rows, 93 MB) and vacuumed; no change.
- **Host contention** — a run started at load average 2.76 collapsed identically.
- **Cache state** — every run flushes Redis and re-warms all 400 (tenant, key) pairs.

The strongest signal is that **achieved throughput is flat at ~390–430 req/s regardless of offered
load** (500, 550, 600 and 750 all land there), which is the signature of a fixed concurrency limit
rather than a resource running out.

---

## 4. The ceiling is not CPU

Sampled during steady state at 500 QPS offered:

| Container | CPU (of 1200%) |
|---|---|
| app (8 workers) | **220%** |
| postgres | 9% |
| redis | 2% |

Roughly 27% of one core per worker. The event loops are mostly **waiting**, not computing — so the
next investigation is a bounded resource, not an optimisation. Leading suspects, untested:

- The default `asyncio.to_thread` executor: `min(32, cpu+4)` = 16 threads per worker, never sized
  deliberately.
- Synchronous Postgres calls on the event loop — `SecretsManagerClient.resolve()` on every live
  fetch, and the control-plane read when its 30s TTL expires across 8 workers at once.
- The async Redis connection pool: 2 cache GETs plus a rate-limiter Lua eval per request.

Supporting evidence: per-source budgets are 80% of the 5s deadline = 4s, and **306 requests came
back partial** — meaning fetches that should take 180ms were taking four seconds. A 180ms
`asyncio.sleep` overshooting by 20× is queueing, not work.

---

## 5. What we changed, and what it bought

### DuckDB, pooled per tenant

| | Before | After |
|---|---|---|
| Acquire an instance | `connect(":memory:")` **6.5ms** | `pool.cursor()` **0.01ms** |
| Federation stage (2-source join, warm) | 13.7–18.0ms | **3.1–5.1ms** |
| Whole request (warm cache) | ~15ms | **~5ms** |

Also applied: `threads=1` (was 12 per instance, per request), `temp_directory=''` (the default
spills tenant rows to disk in plaintext under memory pressure), and a per-tenant `memory_limit`.

Instance footprint measured at ~2.5 MiB, so the default 32-instance LRU costs ~80 MiB per worker.

### In-process join: measured, then rejected

A hand-written Python hash join beat DuckDB by **13–96×** at every cardinality tested — and
**silently disagreed with it** on `ORDER BY` ties at 2,000 rows while passing at 20 and 200. Since
DuckDB executes the *entitled* tree, a second evaluator means the entitlement filter could evaluate
differently depending on result size. Kept one engine. Full reasoning in LOAD-TESTING.md §3.2.

### 8 workers, and the `/metrics` fix they required

`prometheus_client`'s registry is per process, so 8 workers would have made every scrape report one
worker's share. Now uses `PROMETHEUS_MULTIPROC_DIR` with a fresh registry per scrape. Verified: 25
queries across 8 workers scrape as `query_duration_seconds_count 25.0`, not ~3.

---

## 6. What the run proves beyond throughput

- **Tenant isolation, under concurrency.** Every synthetic row carries its tenant's id in a
  projected column; k6 checks every row of every response. **Zero foreign rows in >180,000
  requests across all runs.** This is the claim that most needed testing under load rather than in a unit test.
- **The cache hit ratio is real, not configured.** Measured from `sources[].served` on every
  response: 93.5–95.0% against an intended 95%.
- **Degradation is honest.** Under saturation 306–382 requests returned `partial` with the affected
  source named — no silently truncated joins.
- **The rate limiter never fired** at a 95% hit ratio (0 × 429), which is the point of
  [LOAD-TESTING §3.1](LOAD-TESTING.md)'s arithmetic: 20 tenants at 95% keeps live fetches inside the per-tenant quota.

---

## 7. Next

1. **Find the ~400 req/s concurrency limit.** Instrument the three suspects in §4. This is the
   headline open item — the system is waiting on something bounded, and CPU says there is 5× the
   headroom to reclaim.
2. **Explain run 1**, or stop reporting it. Either it is reproducible under conditions we have not
   identified, or it should come out of the table.
3. **Run S2 and S4 at 500 QPS**, not just 200 — they are currently measured below the knee, so they
   describe the engine rather than its ceiling. And build **S1**, which needs a single-source query.
4. **Re-check the DuckDB cursor-isolation probe** if duckdb is upgraded — the whole per-tenant
   design rests on `register()` being cursor-local, which is verified against 1.5.5 only.
