# Phase 5b — Load Test Execution

> **Goal:** produce `docs/LOAD-RESULTS.md` — the six scenarios in `docs/LOAD-TESTING.md`, run,
> measured and interpreted.
>
> **Depends on:** Phase 5 T501–T505 (per-tenant DuckDB pool, spill hardening, 8 workers +
> multiprocess `/metrics`, load-tenant seeding). This plan **replaces T506** with a task-level
> breakdown and adds two prerequisites that the Phase 5 plan does not contain (T510, T512).
>
> **Assumes:** Phase 4 is reviewed and `REVIEW.md` item 3 (the `asyncio.to_thread` join offload,
> commit `1dcbfaf`) has been accepted. If it is reverted, T510's A/B must be re-measured first.
>
> **Verify:** `make load-mt` produces `docs/artifacts/load/k6-multitenant.txt` with a non-zero
> achieved rate, and `docs/LOAD-RESULTS.md` reports a measured cache hit ratio for S3.
>
> ---
>
> ## STATUS as of 2026-09-28
>
> | Task | State |
> |---|---|
> | T510 `SET threads=1` | **PARTIAL** — measured on *instance creation* only (5.21 -> 4.80ms, 8%). Applied. The execution-under-load A/B was not run. |
> | T511 per-tenant pool | **DONE** — federation 13.7-18.0ms -> 3.1-5.1ms |
> | T512 bound the thread pool | **NOT DONE** — and now the leading suspect for the ~400 req/s ceiling |
> | T513 synthetic datasets | **DONE** — `src/connectors/synthetic.py`, `scripts/seed_load_tenants.py`, 12 tests |
> | T514 k6 scenarios | **PARTIAL** — S3 built and run; S1/S2/S4 specified, not run |
> | T515 Makefile targets | **DONE** — `load-seed`, `load-mt`, with the override-discard guard |
> | T516 summary renderer | **DONE** |
> | T517 run the matrix | **PARTIAL** — S3 at 500/550/600/750 |
> | T518 waterfalls | **DONE** for the canonical query (pool warmed first, or the artifact shows a cold start) |
> | T519 `LOAD-RESULTS.md` | **DONE**, marked in-progress |
>
> **Also done, from the parent Phase 5 plan:** T501/T502 (pool), T503 (spill hard-disabled +
> per-tenant `memory_limit`; the *encrypted* materialization path is NOT built), T504 (multiprocess
> `/metrics` + 8 workers by default), T505 (20 load tenants).
>
> **THE OPEN ITEM.** Achieved throughput is flat at ~390-430 req/s regardless of offered load, with
> app CPU at **220% of 1200%**. The system is waiting on a bounded resource, not computing. Start at
> T512, then the synchronous Postgres calls on the event loop (`SecretsManagerClient.resolve`, the
> control-plane TTL refresh), then the async Redis pool. One 500 QPS run achieved 500.0 with p50
> 141ms and has never been reproduced — either explain it or drop it from the results table.

---

## Sequencing rule

**Every perf change lands before any measured run.** A number measured against code we are about
to change is a number we have to throw away. T510–T513 change what is being measured; T514–T516
build the harness; only T517 onward produces reportable numbers.

---

## File map

| File | Action | Task |
|---|---|---|
| `src/execution/join.py` | modify — `SET threads`, take the pool | T510, T511 |
| `src/execution/duckdb_pool.py` | create | T511 (= Phase 5 T501) |
| `src/main.py` | modify — bound the thread pool | T512 |
| `scripts/seed_load_tenants.py` | create | T513 |
| `src/connectors/synthetic_data.py` | create | T513 |
| `load/query_load.js` | rewrite — six scenarios | T514 |
| `load/lib/zipf.js`, `load/lib/summary.js` | create | T514, T516 |
| `Makefile` | modify — `load-seed`, `SCENARIO`/`RATE`/`ROWS` | T515 |
| `docs/k6-summary.txt` | regenerate | T517 |
| `docs/trace-waterfall-s*.svg` | create | T518 |
| `docs/LOAD-RESULTS.md` | create | T519 |
| `docs/LOAD-TESTING.md` | modify — drop any target that did not get built | T519 |

---

## A. Prerequisites — change what we measure

### - [ ] T510 — Measure `SET threads = 1` in isolation, before anything else

DuckDB's default in-container is `threads = 12`, **per instance, per request**. On 200-row tables
its parallelism buys nothing and costs context switches.

**Decision:** measure it *alone*, before the pool lands, because if it is most of the win then the
pool's headline number is not what it appears to be.

- Add `SET threads = 1` (and keep `SET temp_directory = ''` from T503) at instance creation.
- A/B at a matched 200 RPS offered, 8 workers, warm cache, 2-source join.

**Verify:** `make load SCENARIO=s2 RATE=200` before and after; record achieved / p50 / p95 in the
plan. State both numbers in `LOAD-RESULTS.md` §Learnings regardless of the outcome.

### - [ ] T511 — Per-tenant DuckDB pool (Phase 5 T501 + T502)

Build `src/execution/duckdb_pool.py` and have `join_sources` take a **cursor from the tenant's
instance** instead of calling `duckdb.connect(":memory:")`. Bounded LRU with an explicit cap;
memory is `workers × cached tenants × instance size`.

**Verify:** `pytest -q tests/unit/test_duckdb_pool.py` — including the concurrency probe (N threads
interleaving register+query across tenants, zero cross-tenant rows).

### - [ ] T512 — Bound the thread pool explicitly

`asyncio.to_thread` uses the default executor: `min(32, cpu+4)` = 16 threads **per worker**, so
8 workers give 128 threads on 12 cores.

**Decision:** set an explicit `ThreadPoolExecutor` size in the app factory rather than inheriting
a default that was never chosen. Start at 4 per worker (= 32 total) and sweep 2 / 4 / 8.

**Verify:** `make load SCENARIO=s2` at each size; pick by achieved rate, record the sweep.

### - [ ] T513 — Synthetic per-tenant datasets

`src/connectors/synthetic_data.py` generates rows deterministically from `(tenant_id, row_count)`.

- **Do not touch `mock_data.py`.** The committed 20-row fixtures back existing assertions
  (`SUP-13/SUP-14` tie-break, the CLS masking tests, the canonical-query integration test). A
  separate generator used only by load tenants is more code and zero risk to the suite.
- Each tenant's rows carry **its own id as a stamp** in a text column, so a cross-tenant leak is
  greppable in a k6 response check.
- `ROWS` controls cardinality per source; the key space is the distinct `repo` literals.

`scripts/seed_load_tenants.py` creates **20** tenants (`tenant_load_00`…`19`) with grants,
budgets, secrets and policies mirroring `tenant_load`.

**Verify:** `pytest -q tests/unit/test_synthetic_data.py` — determinism, stamp uniqueness, exact
row counts; then `make load-seed && make test-integration -k load_tenants`.

---

## B. The harness

### - [ ] T514 — Rewrite `load/query_load.js` into six scenarios

One file, six exported scenario functions, **zero remote imports** (ADR-039 stands).

| Scenario | Executor | Target | Staleness | Notes |
|---|---|---|---|---|
| s0 | constant-arrival-rate | `GET /healthz` | — | control |
| s1 | constant-arrival-rate | single-source SQL | 60000 | warmed in `setup()` |
| s2 | constant-arrival-rate | canonical join | 60000 | warmed in `setup()` |
| s3 | constant-arrival-rate | canonical join, 20 tenants | 60000 | **Zipf** key draw over a key space wider than the warm set |
| s4 | constant-arrival-rate | canonical join | **0** | expects 200 **and** 429 |
| s5 | constant-arrival-rate | canonical join | 60000 | `/v1/test/fail-next` armed on a timer |

Required additions:
- `load/lib/zipf.js` — seeded Zipf sampler, no dependencies.
- A custom `Rate('cache_hit')` fed from `sources[].served` in the response envelope — the results
  document prints **measured** hit ratio, never intended.
- A `check` on every S3 response that no **foreign tenant stamp** appears in any row.
- 10s warm-up per scenario via `startTime`, tagged and excluded from the reported window.
- `http.expectedStatuses(200, 429)` on S4 so its deliberate 429s are not counted as failures.
- Thresholds present but **not tuned to pass**.

**Verify:** `make load SCENARIO=s0 DURATION=10s` completes and reports achieved ≈ offered.

### - [ ] T515 — Makefile targets

`load-seed`; `load` accepting `SCENARIO`, `RATE`, `ROWS`, `DURATION`; `load` runs all six plus the
cardinality sweep when `SCENARIO` is unset.

**Watch-out:** `docker compose run` honours `depends_on` and **recreates the dependency from the
base compose file, silently discarding `-f override.yml`**. This already produced one wrong
measurement. Any run needing an override must use `docker run --network <compose-net>`, and the
target must assert the worker count it actually got (read `/proc/1/cmdline` in the app container).

**Verify:** `make load SCENARIO=s2 RATE=100 DURATION=10s ROWS=200` honours all four.

### - [ ] T516 — Summary renderer

`load/lib/summary.js`, rendered in `handleSummary` from `data.metrics[...].values`. One row per
scenario: offered / achieved / dropped / p50 / p90 / p95 / max / error rate / measured hit ratio /
429 rate.

**Verify:** `docs/k6-summary.txt` contains one row per scenario and no ANSI escapes.

---

## C. Execution and reporting

### - [ ] T517 — Run the matrix

All six scenarios at 500 QPS / 60s / 8 workers, plus the cardinality sweep (20 / 200 / 2,000) at
S2's settings. `OTEL_EXPORTER=none` throughout.

**Verify:** `docs/k6-summary.txt` has six scenario rows and three sweep rows, all with non-zero
achieved rates.

### - [ ] T518 — Waterfalls

One per scenario, from a **separate low-rate traced run** (span export distorts the latency being
measured). Label each artifact with the run it came from.

**Verify:** `make trace SCENARIO=s2` writes `docs/trace-waterfall-s2.svg` with all seven spans
resolved — the renderer already fails loudly on an incomplete trace.

### - [ ] T519 — Write `docs/LOAD-RESULTS.md`

Crisp and high level. Sections:

1. **One-line verdict** per scenario — held / degraded / failed, against the brief's SLO.
2. **The results table** (T516's output, as-is).
3. **The cardinality curve** — how the join scales with data.
4. **Learnings**, and these must include the ones that were *wrong*:
   - the audit INSERT was predicted to be the P95 and measured at 0.247ms;
   - a `--workers 8` measurement was invalid because `docker compose run` discarded the override;
   - the T510 and T512 sweeps, whatever they showed.
5. **Where it breaks** — the knee, and what to fix next.

Then **reconcile `LOAD-TESTING.md`**: delete any documented `make` target or scenario that did not
get built. The two documents ship together and must not disagree.

**Verify:** every command in `LOAD-TESTING.md` §7 runs on a fresh `make up`.

---

## Verification gate

```bash
make up && make seed && make load-seed
make load                       # six scenarios + sweep
make trace SCENARIO=s2
make scrape
```

Gate passes when:
- [ ] `docs/k6-summary.txt` reports all six scenarios with achieved rates and dropped counts.
- [ ] S3's **measured** cache hit ratio is reported and within 2pp of the intended 95%.
- [ ] S3 logs **zero** foreign tenant stamps across the full run.
- [ ] S4 returns 429s with `Retry-After`, and its 200s stay inside P95 < 1.5s.
- [ ] S5 returns `partial` with `join_status: incomplete` and `next_cursor: null`.
- [ ] `docs/LOAD-RESULTS.md` states the knee and at least one prediction that was wrong.
- [ ] 591+ unit and 100+ integration tests still pass; ruff clean; no file ≥ 500 lines.

---

## Open questions

1. **Is 500 achieved a goal or a measurement?** Today's knee is ~200–240 RPS at 8 workers.
   T510–T512 plausibly reach ~450–500, but it is marginal and machine-dependent. Recommendation:
   hold the offered rate at 500 and report achieved honestly — the brief says "~500–1k QPS, local
   acceptable", and a credible capacity curve with a stated knee beats a tuned number.
2. **Waterfall under load, or at rest?** A traced run at 500 QPS would show queueing, which is more
   interesting — but only if file export costs <2% of p50. That is a measurement to make in T518,
   not a decision to guess now.
3. **Does S5 belong at 500 QPS?** Degradation under saturation and degradation under normal load
   are different findings. Possibly run S5 at the S3 knee instead, and say so.
