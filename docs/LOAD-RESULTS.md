# Load Test Results

> How to reproduce: **[LOAD-TESTING.md](LOAD-TESTING.md)** · Raw k6 summaries: `docs/artifacts/load/`

**Status: measured, not tuned.** Below is what the system does today; §4 is what we would do next.

---

## 1. Verdict

| Question | Answer |
|---|---|
| What does a warm two-source entitled join cost? | **24.8ms p50** — 20× inside the 500ms SLO |
| Does it hold ~500 QPS? | **Not reproducibly.** The ceiling is **~400 req/s**, and at that load latency falls outside SLO |
| Is the ceiling CPU? | **No.** The app peaks at **220% of 1200%** available |
| Does tenant isolation hold under load? | **Yes.** Zero foreign rows across >180,000 requests |
| Does the cache behave as designed? | **Yes.** Measured 93.5–95.0% against an intended 95% |
| Does it degrade honestly when saturated? | **Yes.** Partials labelled and cursor withheld; no wrong answers |
| Does the rate limiter bind at a real quota? | **Yes.** 761 × `429`, and served requests stay inside SLO |

---

## 2. Measured

8 workers on a 12-core laptop, 20 tenants, 200 rows per source.

| Scenario | Offered | Achieved | p50 | p95 | Hit ratio |
|---|---|---|---|---|---|
| **S2** — 100% hit, the engine alone | 200 | 200.0 | **24.8ms** | 164.7ms | 100% |
| **S3** — ~95% hit, realistic mix | 500 | ~400–427 | ~4s | ~6s | 93.5–95.0% |
| **S4** — 0% hit, GitHub's real quota | 200 | 200.1 | 196.7ms | 378.8ms | 0% |

**S2 is the number worth quoting.** A two-source entitled join — parsed, entitled, planned, fetched
from two connectors and joined — costs **24.8ms p50** cache-warm, 20× inside the brief's 500ms budget.

**S3 is where it stops.** Offered 500 QPS, the system achieves ~400 and latency degrades to seconds,
outside SLO. One run *did* hold 500.0 req/s at p50 141ms; five subsequent attempts did not reproduce
it, so we do not claim it.

**S4 is a designed failure.** At a 0% hit ratio against GitHub's real 1.39 req/s per-tenant quota,
761 of 3,000 requests are refused with `429` + `Retry-After` — and the requests that *are* served
stay inside SLO. The system refuses work it cannot do rather than collapsing.

*(S1, single-source, is specified but not built.)*

---

## 3. The ceiling is not CPU

At 500 QPS offered: app **220% of 1200%** available, Postgres 9%, Redis 2%. The event loops are
**waiting, not computing** — so roughly 5× headroom sits behind a bounded resource.

Achieved throughput is **flat at ~390–430 req/s regardless of offered load** — 500, 550, 600 and 750
all land there. That is the signature of a fixed concurrency limit, not of a resource running out.
Corroborating it: fetches that should take 180ms were taking four seconds, and a 180ms
`asyncio.sleep` overshooting by 20× is queueing, not work.

---

## 4. What we would do next

1. **Find the concurrency limit.** Three untested suspects, in order: the default `asyncio.to_thread`
   executor (16 threads per worker, never sized deliberately), synchronous Postgres on the event loop
   (`SecretsManagerClient.resolve()` runs on every live fetch), and the async Redis pool.
2. **Re-run S2 and S4 at 500 QPS**, and build S1. Both are currently measured at 200 — below the
   knee — so they describe the engine rather than its ceiling.
3. **Explain the one 500 QPS run, or stop recording it.** Audit-table growth, host contention and
   cache state are already ruled out.
4. **Sweep cardinality.** Every join number comes from a single data size; 20 / 200 / 2,000 rows per
   source would turn "the join is cheap" into a curve.
5. **Build encrypted materialization.** Spilling is hard-disabled today (`temp_directory=''`), which
   is the safe half — an oversized join fails loudly instead of writing tenant rows to disk in
   plaintext. The opt-in per-tenant encrypted spill (design-doc §4.4) is unbuilt.

---

## 5. Two notes on what these numbers are

**The join runs in DuckDB, one pooled instance per tenant.** A hand-written in-process join measured
faster, and we kept DuckDB anyway: it executes the *entitled* tree — the RLS predicate and CLS mask
are compiled into that AST — so a second evaluator would be a second implementation of entitlement.
Worth revisiting if a profile ever shows the join as the constraint; it currently doesn't.

**These measure the federate-live path only.** Joins execute in worker memory. The short-lived
materialization half of the join strategy (design-doc §4.4) is designed and not built, so no number
here describes it.
