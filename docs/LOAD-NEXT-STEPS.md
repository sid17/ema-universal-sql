# Load Testing — Next Steps

> Picking this up cold? Read **[LOAD-RESULTS.md](LOAD-RESULTS.md)** first — it has the numbers and
> what they mean. This file is only what is left to do, in priority order.
>
> Everything below is reproducible with `make load-mt`. See
> [LOAD-TESTING.md §7](LOAD-TESTING.md) for the flags.

---

## 0. Where you are

Committed and green: **617 unit + 100 integration tests, ruff clean, working tree clean.**

```
12d81b5  docs: the load-test method, the results, and what is left
42540ec  refactor(docs): group generated artifacts, and trace the warm path
94b4592  feat(load): a realistic multi-tenant k6 profile at 500 QPS
b0265a6  feat(connectors): per-tenant synthetic datasets, and the load tenants
517dd5d  feat(observability): multiprocess /metrics, and 8 workers by default
9c0fc6c  perf(execution): pool one DuckDB instance per tenant
```

Resume with:

```bash
make up && make seed && make load-seed
make load-mt                 # S3, the realistic scenario
```

---

## 1. Find the ~400 req/s ceiling — THE open item

Achieved throughput is flat at **390–430 req/s regardless of offered load** (500, 550, 600 and 750
all land there), while **app CPU peaks at 220% of 1200% available**. The event loops are waiting,
not computing. That is a bounded resource, and roughly **5× headroom** to reclaim.

Test the three suspects in this order — each is a small, independent change:

| # | Suspect | Why | How to test |
|---|---|---|---|
| 1 | **`asyncio.to_thread` executor** | Defaults to `min(32, cpu+4)` = 16 threads per worker. Never sized deliberately; 8 workers = 128 threads on 12 cores. | Set an explicit `ThreadPoolExecutor` in the app factory. Sweep 2 / 4 / 8 / 16 at `make load-mt RATE=500`. |
| 2 | **Synchronous Postgres on the event loop** | `SecretsManagerClient.resolve()` runs on every live fetch; the control-plane read blocks when its 30s TTL expires across 8 workers at once. | Time both under load. Move to `to_thread` or an async driver if either shows up. |
| 3 | **Async Redis connection pool** | 2 cache GETs + a rate-limiter Lua eval per request, all through one client per worker. | Raise `max_connections` explicitly and re-measure. |

**The strongest clue:** per-source budgets are 4s (80% of the 5s deadline), and 306–382 requests came
back `partial` — meaning fetches that should take 180ms took four seconds. A 180ms `asyncio.sleep`
overshooting by 20× is queueing, not work. Whatever is blocking is upstream of the connector call.

---

## 2. Explain the 500 QPS run, or drop it

One run achieved **500.0 req/s, 0 dropped, p50 141ms, p95 447ms** — comfortably inside the SLO.
Five subsequent runs at the same offered rate landed at 400–427 req/s with p50 ~4s.

Already ruled out: audit-table growth (truncated 168k rows, no change), host contention (a run at
load average 2.76 collapsed identically), cache state (every run flushes Redis and re-warms all 400
pairs).

Either it is reproducible under conditions we have not identified, or **it comes out of the results
table**. It should not stay there as an unexplained best case.

---

## 3. Finish the scenario set

- **Build S1** (single source, 100% hit). Needs a single-source query in `load/multitenant.js` —
  everything else is in place. It is the floor that isolates the join cost when compared to S2.
- **Run S2 and S4 at 500 QPS.** Both are currently measured at 200 QPS, which is below the knee, so
  they describe the engine rather than its ceiling.
- **Cardinality sweep** — 20 / 200 / 2,000 rows per source at S2's settings
  (`make load-mt SYNTHETIC_ROWS=2000`). Turns "the join costs ~2ms" into a curve. Currently every
  join number is measured at a single data size.

---

## 4. Smaller, still worth doing

- **The `threads=1` A/B under load.** Measured only on *instance creation* (5.21 → 4.80ms, 8%). It
  was applied on the reasoning that one scheduler thread per core per instance multiplies badly with
  an instance per tenant — but that reasoning is untested at load.
- **Encrypted materialization is not built.** Spilling is hard-disabled (`temp_directory=''`), which
  is the safe half. The opt-in encrypted-spill path — `ATTACH` with a key derived from the tenant's
  `fernet_key` — is designed in the Phase 5 plan and unimplemented. It is what would make brief line
  96 real rather than described.
- **Re-run the DuckDB cursor-isolation probe if duckdb is upgraded.** The entire per-tenant design
  rests on `register()` being cursor-local, verified against **1.5.5 only**.
  `tests/unit/test_duckdb_pool.py::test_two_cursors_on_one_instance_cannot_see_each_other` is the
  guard — if it ever fails, the pool is unsafe.
- **Two files are past LAW 1's 400-line decompose threshold** (under the 500 hard limit):
  `src/connectors/mock_adapter.py` 413, `tests/unit/test_entitlement.py` 404.

---

## 5. Traps that already cost time

Recorded because each one produced a wrong or meaningless result that looked fine:

1. **`docker compose run` discards `-f` overrides.** It honours `depends_on` and recreates `app`
   from the base compose file. A whole run was measured with `SYNTHETIC_ROWS=0` — every query joined
   an empty dataset and every check still passed. `make load-mt` now asserts the config it got.
2. **`src/` and `scripts/` are baked into the image, not bind-mounted.** Without `--build` a run
   measures the previous code. This silently ignored a rate-limit change.
3. **A fixed burst masks the quota.** Burst was 500 against an 84-request budget, so capacity was
   584 and the limiter never fired — S4 reported 0 × 429 and looked like a pass. Burst is now
   derived as one tenth of the budget.
4. **`__VU`-derived indexing does not enumerate what you think.** VU ids are allocated globally
   across k6 scenarios, so the warm-up skipped pairs and the steady phase reported a mystery 5% miss
   rate that looked like a cache bug. Now uses `exec.scenario.iterationInTest`.
5. **Never `git reset --hard` with uncommitted work.** It destroyed an hour of work in an earlier
   session — `git stash` or `git checkout -- <file>` instead. Related: `git commit` commits the
   whole index, so a `git mv` staged earlier gets swept into an unrelated commit.
6. **Trace the *warm* path.** A waterfall captured on a worker's first query for a tenant pays the
   6.5ms instance creation and renders `duckdb_join` at ~58ms — 28× its steady-state cost.
   `scripts/trace.sh` warms the pool before truncating the span log.

---

## 6. Where things are

| | |
|---|---|
| Method | `docs/LOAD-TESTING.md` |
| Results | `docs/LOAD-RESULTS.md` |
| Artifacts | `docs/artifacts/{load,trace,metrics,demo}/` |
| Task-level plan | `docs/phoenix-development-workflow/plans/2026-09-28-phase5b-load-test-execution.md` (has the STATUS table) |
| k6 profile | `load/multitenant.js` |
| The pool | `src/execution/duckdb_pool.py` + `tests/unit/test_duckdb_pool.py` |
| Synthetic data | `src/connectors/synthetic.py`, `scripts/seed_load_tenants.py` |
