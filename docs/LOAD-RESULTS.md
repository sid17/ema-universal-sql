# Load Test Results

> How to reproduce: **[LOAD-TESTING.md](LOAD-TESTING.md)** · Raw k6 summaries: `docs/artifacts/load/`

All five runs below are from one session: 8 workers on a 12-core laptop, 20 tenants, 200 rows per
source, 60s steady state after an excluded warm-up. Every number here matches a committed artifact.

---

## 1. Verdict

| Question | Answer |
|---|---|
| Does it hold the brief's rate? | **Clean to 400 req/s** — everything offered served, both SLO bounds met |
| Where does it stop? | Between 400 and 500. At 500 offered it achieves **415** and p50 degrades to 4.3s |
| Is the ceiling CPU? | **No.** The app peaks at **220% of 1200%** available |
| Does tenant isolation hold under load? | **Yes.** Zero foreign rows across every run |
| Does the cache behave as designed? | **Yes.** 95.06% and 95.19% measured against an intended 95% |
| Does it degrade honestly when saturated? | **Yes.** 340 partials, correctly labelled; no wrong answers |
| Does the rate limiter bind at a real quota? | **Yes.** 8,508 × `429`, zero failed checks |

---

## 2. Measured

| Offered | Achieved | Dropped | p50 | p95 | Hit ratio | 429s | Partials | Leaks |
|---|---|---|---|---|---|---|---|---|
| 200 *(100% hit)* | 200.0 | 0 | **94.2ms** | 472.9ms | 100% | 0 | 0 | 0 |
| 300 | 300.0 | 0 | **141.9ms** | 523.6ms | 95.06% | 0 | 0 | 0 |
| **400** | **400.0** | **0** | **195.1ms** | **552.9ms** | 95.19% | 0 | 0 | 0 |
| 500 | 414.9 | 5,110 | 4,289.6ms | 5,818.3ms | 93.81% | 0 | 340 | 0 |
| 200 *(0% hit, real quota)* | 200.0 | 0 | 6.0ms | 248.0ms | 0% | **8,508** | 0 | 0 |

**The headline: 400 req/s, clean.** Everything offered is served, nothing is dropped, no check
fails, and both SLO bounds hold with room — p50 195ms against a 500ms budget, p95 553ms against 1.5s.

**It is a cliff, not a curve.** 400 serves everything; 500 drops 5,110 iterations and p50 jumps 22×.
Latency rises gently from 200 → 400 (94 → 142 → 195ms) and then falls off. That shape is a fixed
concurrency limit rather than a resource gradually running out.

**Saturation stays honest.** At 500 offered, 340 responses come back `partial` with the affected
source named, and 1,259 of 73,235 checks fail — the join-completed assertion on those degraded
responses, plus requests that blew the 5s deadline. **Zero tenant leaks in every run.** The system
sheds work and says so; it does not return wrong answers.

**The quota scenario does what it should.** At a 0% hit ratio against GitHub's real 1.39 req/s
per-tenant budget, the limiter refuses **8,508 of 12,001** requests with a clean `429` and zero
failed checks. Note its 6.0ms p50 is *the throttle path*, not the serve path — most requests are
being refused. The p95 of 248ms is the honest read on requests actually served.

---

## 3. The ceiling is not CPU

At 500 QPS offered: app **220% of 1200%** available, Postgres 9%, Redis 2%. The event loops are
**waiting, not computing**, so roughly 5× headroom sits behind a bounded resource — which is
consistent with the cliff above, and with fetches that should take 180ms taking four seconds. A
180ms `asyncio.sleep` overshooting by 20× is queueing, not work.

---

## 4. What we would do next

1. **Find the concurrency limit.** Three untested suspects, in order: the default `asyncio.to_thread`
   executor (16 threads per worker, never sized deliberately), synchronous Postgres on the event loop
   (`SecretsManagerClient.resolve()` runs on every live fetch), and the async Redis pool. The cliff
   between 400 and 500 is where to instrument.
2. **Bisect 400→500.** The knee is somewhere in that 100 req/s band; 450 would halve it again.
3. **Sweep cardinality.** Every number here comes from 200 rows per source. 20 / 2,000 would turn
   "the join is cheap" into a curve.
4. **Soak it.** 60s exercises no memory growth, cache eviction under pressure, or pool exhaustion.
5. **Build encrypted materialization.** Spilling is hard-disabled today (`temp_directory=''`), which
   is the safe half — an oversized join fails loudly instead of writing tenant rows to disk in
   plaintext. The opt-in per-tenant encrypted spill is designed and unbuilt.

---

## 5. Two notes on what these numbers are

**The join runs in DuckDB, one pooled instance per tenant.** A hand-written in-process join measured
faster, and we kept DuckDB anyway: it executes the *entitled* tree — the RLS predicate and CLS mask
are compiled into that AST — so a second evaluator would be a second implementation of entitlement.
Worth revisiting if a profile ever shows the join as the constraint; it currently doesn't.

**These measure the federate-live path only.** Joins execute in worker memory. The short-lived
materialization half of the join strategy is designed and not built, so no number here describes it.
