# Phase 5 — Per-tenant isolation, spill encryption, and honest load numbers

> **Read this first if you are picking the work up.** This phase did not come from a file in
> `docs/design/phases/`. It came out of **measuring Phase 4's load run**, which found a bottleneck nobody had
> predicted and, while investigating it, a real plaintext-on-disk leak path. Every number below was measured
> on this machine and is reproducible; none of it is estimated.
>
> **Status: designed and approved, NOT started.** No code in this document has been written.

---

## 0. STATUS — 2026-09-28

**Phase 4 is committed** (12 commits ending `837a9e2`). The §0 stop sign below is therefore spent;
it is kept for the record. T501-T505 are **built**; T506 is superseded by
`2026-09-28-phase5b-load-test-execution.md`, which carries the live status table. The encrypted
materialization half of T503 is **not** built — spilling is hard-disabled by default, which is the
safe half; the opt-in encrypted path remains designed only.

---

## 0b. The original stop sign — read this before you touch anything

**Nothing from Phase 4 is committed.** `git status` shows ~42 entries (22 modified, 20 new). The whole of
Phase 4 — spans, histograms, the gauge feed, simulated latency, the waterfall renderer, the k6 profile, the
README rewrite — is sitting in the working tree waiting on the user's review of
`docs/phoenix-development-workflow/REVIEW.md`.

**Do not start Phase 5 until Phase 4 is reviewed and committed.** Two reasons:

1. Phase 5 edits `src/execution/join.py`, `src/observability/metrics.py`, `docker-compose.yml`,
   `Dockerfile` and `config/*.yaml` — all of which Phase 4 already touched. Mixing them makes the Phase 4
   diff unreviewable.
2. **REVIEW.md item 3 flags an unapproved change**: Phase 4 moved the DuckDB join off the event loop with
   `asyncio.to_thread` in `src/execution/federation.py`. The user has not accepted or rejected it. Phase 5's
   design assumes it stays. If it is reverted, re-measure before building anything here.

---

## 1. What the measurement actually found

### 1.1 The engine is not the cost — DuckDB instance creation is

Stage timings on a **warm cache**, where `connector_ms: {}` means no connector call happened at all:

| stage | single-source | 2-source join |
|---|---|---|
| parse | 1.0–1.2ms | 1.4–2.9ms |
| entitlement | 0.02ms | 0.02–0.62ms |
| plan | 0.10ms | 0.11–0.13ms |
| **federation** | **11.2–13.7ms** | **13.7–18.0ms** |
| assemble | 0.40ms | 0.57–1.76ms |

So federation costs ~12ms with **zero** connector work. Decomposing it (in-container, n=100–200):

| | p50 | p95 |
|---|---|---|
| `duckdb.connect(":memory:")` + `close()`, **nothing else** | **6.49ms** | 7.51ms |
| full register + execute + read-back, **fresh** connection (today's code) | 8.28–8.34ms | 9.26–10.18ms |
| full cycle, **cursor off a reused connection** | **1.21–1.48ms** | 1.66–1.86ms |

**Creating a DuckDB instance per request is ~78% of the join stage** and roughly half of a cache-hit request.
The entire query engine — SQL validation, RLS/CLS compilation, pushdown planning, envelope assembly — is
~1.6ms. Reuse is a **6.8× improvement on that stage**.

The `:memory:`-per-request pattern came from `01-EXECUTION-PLAN.md` §D research Card 3 (universql). It is the
right *isolation* answer and an expensive *per-request* one. Phase 5 keeps the isolation and drops the cost.

### 1.2 The phase file's predicted bottleneck was wrong

`phase-4-observability.md` asserted the synchronous audit `INSERT` "**is** the P95" at 500 QPS. Measured:

| synchronous work, per request | p50 | p95 |
|---|---|---|
| DuckDB join (fresh connection) | 10.29ms | 11.12ms |
| audit `INSERT` | **0.247ms** | 0.432ms |
| sqlglot parse | 0.243ms | 0.307ms |

Audit INSERT under concurrency (`synchronous_commit=on`, `max_connections=100`):

| threads | throughput | p50 |
|---|---|---|
| 1 | 3,939/s | 0.22ms |
| 4 | 4,348/s | 0.84ms |
| 8 | 2,222/s | 3.45ms |
| 16 | 2,059/s | 7.63ms |

Postgres is nowhere near a bottleneck at these rates. **ADR-042's measure-first gate was correct and
T411 (batch the audit write) stays closed as NOT DONE.** Do not reopen it without new evidence.

### 1.3 The capacity curve

`sweep.js`, 15s per point, warm cache, `max_staleness_ms=60000`, 2-source join unless noted:

| offered RPS | 1 worker | 8 workers | 8 workers, single-source |
|---|---|---|---|
| 25 | 24.6 · p50 20ms | — | — |
| 50 | 49.2 · p50 20ms · p95 42ms | 49.0 · p50 19ms | — |
| 100 | 61.1 · p50 **5,164ms** · 181 dropped | 97.7 · p50 22ms | — |
| 150 | 65.3 · p50 **4,450ms** · 549 dropped | — | — |
| 200 | 67.8 · p50 **7,989ms** · 923 dropped | **196.6 · p50 35ms · p95 259ms** | **198.8 · p50 19ms · p95 38ms** |
| 400 | — | 239.0 · p50 3,748ms · 1,281 dropped | 338.3 · p50 1,389ms · 437 dropped |
| 500 | — | — | 327.8 · p50 2,611ms · 1,374 dropped |

**Knee: ~50–65 RPS on one worker, ~200–240 RPS on eight.** At 200 RPS with 8 workers the join query is
p50 35ms / p95 259ms — inside the brief's SLO (P50 < 500ms, P95 < 1.5s) by 14× and 6×.

Control experiments proving the harness and framework are not the limit:

| endpoint | offered | achieved | p50 | dropped |
|---|---|---|---|---|
| `GET /healthz` (touches Postgres + Redis) | 500 | **500.0** | 1.1ms | 0 |
| `POST /v1/auth/mock-token` (pure CPU, 1 worker) | 500 | **499.9** | 1ms | 0 |

During a saturating 150 RPS run on one worker: **app CPU ~245% of 1200% available**, Postgres 3%, Redis 1%.
Nothing was saturated — the limit was one event loop's worth of ~15ms-per-request synchronous work.

### 1.4 The `asyncio.to_thread` result (Phase 4, uncommitted)

Matched A/B at 200 RPS offered, 1 worker:

| | before | after |
|---|---|---|
| achieved | 48.3 RPS | **63.1 RPS** (+31%) |
| p(50) | 21,850ms | **6,767ms** (−69%) |
| p(95) | 29,147ms | **20,959ms** (−28%) |

---

## 2. Two isolation facts that decide the design

Both measured against **duckdb 1.5.5**, in-container.

### 2.1 `register()` is cursor-local — verified, not assumed

| probe | result |
|---|---|
| alice's cursor and bob's cursor, same instance, same view name, different rows | each sees **only its own rows** |
| a third cursor that registered nothing | **`CatalogException`** — not someone else's data |
| 8 threads × 250 interleaved register+query on ONE instance (2,000 queries) | **0 cross-user bleeds** |

So **one DuckDB instance per tenant + one cursor per request** is safe for concurrent users *within* a tenant.
This is what makes the design viable. **Re-run this probe if duckdb is ever upgraded** — the whole isolation
argument rests on it.

### 2.2 The current code has a live plaintext-on-disk path

DuckDB `:memory:` defaults, measured:

```
temp_directory          = '.tmp'
max_temp_directory_size = '90% of available disk space'
memory_limit            = '9.3 GiB'
threads                 = 12
```

**An in-memory DuckDB database spills to `.tmp/` on disk, in plaintext, under memory pressure.** Our datasets
are 14 and 9 rows so it has never fired, but it is a real leak path in code that is running today, not a
hypothetical one. Confirmed by control: an unencrypted file-backed DuckDB database leaves
`acme-confidential` findable with `grep` in the raw file.

Two things are available to fix it:

| | result |
|---|---|
| `SET temp_directory = ''` | accepted; setting reads back as `''` — spilling is hard-disabled |
| `ATTACH '<path>' AS x (ENCRYPTION_KEY '<key>')` | **supported.** 536,576B file, plaintext **absent**; wrong key → `InvalidInputException: Wrong encryption key used to open the database` |

---

## 3. Decisions (user-approved, 2026-09-28)

### D1 — Per-tenant DuckDB instances, safe by default, encrypted when materializing

**Rejected:** one shared process-wide instance with per-request cursors. The user's call, and the right one —
the isolation boundary should be the database instance, not a cursor convention, even though §2.1 shows
cursors do isolate. Defence in depth, and it matches HLD §2's per-tenant-namespace stance.

**Default (`MATERIALIZE=off`):**
```
SET temp_directory = ''       -- no tenant byte ever reaches disk unencrypted
SET memory_limit   = <bound>  -- one tenant cannot exhaust the process
```
A join too big for memory then fails **loudly** rather than silently writing plaintext.

**Opt-in (`MATERIALIZE=encrypted`):** spill into a per-tenant DuckDB attached with an `ENCRYPTION_KEY`
**derived from that tenant's existing `tenants.fernet_key`**. Because the key is per-tenant and lives outside
the file, **crypto-shred then destroys materialized data too** — the same property `test_crypto_shred`
already proves for secrets, extended to spill. This also makes the brief's line 96 ("short-lived tables …
encrypted per tenant") real rather than described.

> **Design point to get right:** do **not** hand `tenants.fernet_key` to DuckDB raw. Derive a distinct
> spill key from it (HKDF, or at minimum a domain-separated hash) so the materialization key and the
> secrets-encryption key are not the same material. Same root of trust, different derived keys.

### D2 — 8 uvicorn workers become the default

Measured ~4× throughput. Costs, all of which are in scope:

- **`/metrics` breaks.** `prometheus_client`'s default registry is per-process, so a scrape returns whichever
  worker answered. Needs `PROMETHEUS_MULTIPROC_DIR` + `MultiProcessCollector`, a fresh `CollectorRegistry()`
  built **per scrape**, and the directory wiped at startup. **This is a real amendment to ADR-015**, which
  says "/metrics is one route we own calling `generate_latest(REGISTRY)`" — the route stays ours, the
  registry changes. Write the amending ADR.
- `Gauge` needs an explicit `multiprocess_mode` (`livesum`/`max`/…) — `rate_limit_remaining` must pick one.
  Histograms work unchanged. `process_*` and `python_gc_*` collectors do not work in multiprocess mode.
- Each worker keeps its own control-plane TTL cache → N× the control-plane read rate.
- **Per-tenant DuckDB instances are per-process**, so memory is `workers × cached tenants × instance size`.
  The pool bound must account for that.

### D3 — Seed ~20 synthetic load tenants

`tenant_load_00…19` with large budgets. This is what turns *"connector quota is per-tenant so it is not a
throughput bottleneck"* from an assumption into a measurement, and it is the only way the load test exercises
per-tenant buckets, per-tenant caches, per-tenant DuckDB instances **and the pool's eviction path** under load.

---

## 4. Task list

### - [ ] T501 — `src/execution/duckdb_pool.py`: the per-tenant instance cache

**Design:** an LRU keyed by `tenant_id` holding open `DuckDBPyConnection` objects. Bounded (suggest 32) with
idle eviction (suggest 10 min — the brief's "lifecycle ≤ N minutes"). Eviction **closes** the connection and,
under `MATERIALIZE=encrypted`, deletes the tenant's spill file. Thread-safe under a plain `threading.Lock`:
it is called from inside `asyncio.to_thread`, so it is genuinely multi-threaded, not just concurrent.

On instance creation, apply §3 D1's hardening. Expose `connection_for(tenant_id)` returning a context manager
that yields a **cursor** and closes it in a `finally`.

**Verify:** `pytest tests/unit/test_duckdb_pool.py` —
- two tenants get **different** instance objects;
- tenant A's registered table is invisible from tenant B's cursor (`CatalogException`);
- the same tenant reuses one instance across calls;
- eviction closes the connection and a subsequent call transparently reopens;
- the bound is honoured under 100 distinct tenants;
- concurrent access from 8 threads produces no bleeds (port the §2.1 probe into a real test).

### - [ ] T502 — `join_sources` takes the pool instead of `duckdb.connect`

**Files:** `src/execution/join.py`, `src/execution/federation.py`

`tenant_id` must reach `join_sources`; `FederationEngine.execute` already has it, so thread it through.
Keep the `duckdb_join` span exactly where it is — the waterfall artifact and
`tests/integration/test_trace.py` both depend on the span name and its parenting.

**Verify:** whole unit + integration suite unchanged; then re-measure the warm-cache stage breakdown and
confirm `federation_ms` drops from ~12ms to ~5ms.

### - [ ] T503 — spill hardening + encrypted materialization

**Files:** `src/config.py` (`MATERIALIZE`, `DUCKDB_MEMORY_LIMIT`, spill dir), `src/execution/duckdb_pool.py`,
key derivation next to `src/governance/secrets.py`.

`ControlPlaneRepository.get_tenant(tenant_id)` returns a `Tenant` carrying `.fernet_key` — that is the root
key. `SecretsManagerClient` is the model to follow for "resolve a per-tenant key and never log it".

**Verify:** `pytest tests/unit/test_duckdb_spill.py` —
- with `MATERIALIZE=off`, `current_setting('temp_directory')` is `''`;
- a deliberately oversized join raises rather than writing anything under the spill dir;
- with `MATERIALIZE=encrypted`, forcing a spill writes a file **and** `grep` for a known row value finds
  **nothing** in it (mirror the §2.2 control so the test can actually fail);
- attaching that file with the wrong key raises `InvalidInputException`;
- evicting the tenant removes the file.

### - [ ] T504 — multiprocess `/metrics`, then 8 workers by default

**Files:** `src/observability/metrics.py`, `src/gateway/routes.py`, `src/main.py`, `Dockerfile`,
`docker-compose.yml`, new ADR amending ADR-015.

Order matters: **make `/metrics` correct under multiprocess first, prove it, and only then raise the worker
count.** Reversing that ships a silently wrong `/metrics`.

**Verify:** `tests/integration/test_metrics.py` must still pass **with 8 workers running** — specifically
`test_the_gauge_matches_what_the_envelope_told_the_caller`, which is the one that catches a per-process
registry (the envelope comes from one worker, the scrape possibly from another). Add a test that
`query_duration_seconds_count` aggregates across workers rather than reporting one worker's slice.

### - [ ] T505 — seed the load tenants

**Files:** `config/rate_limits.yaml`, `config/grants.yaml`, tenant rows (see
`migrations/002_seed_tenants.sql`), `scripts/seed.py`.

Note `scripts/seed.py` already has `_revoke_grants_not_in_config`, so grants prune correctly — check whether
tenants need the same treatment or the 20 rows are additive-only.

**Verify:** `pytest tests/integration/test_seed.py`; all 20 tenants can mint a token and run the canonical
query without a 403.

### - [ ] T506 — the load-test methodology and scenarios

**Not designed yet — this is the conversation that was in progress when the session ended.** The user's
stated shape:

1. one tenant, **everything hits cache** — isolates the engine;
2. a realistic **distribution** across the 20 tenants, with a mixed hit/miss ratio;
3. separate DuckDB-join vs in-memory/single-source scenarios;
4. report all of them rather than one headline number.

The user explicitly asked for **online research on load-test methodology** before settling this (open-vs-closed
workload models, why `constant-arrival-rate` is the right executor, how to pick and justify a hit ratio,
steady-state vs cold-start, and how to report `dropped_iterations` honestly). That research has **not** been
done. Do it before writing scenarios.

### - [ ] T507 — update the artifacts and the README

`docs/k6-summary.txt`, the capacity table in README *The load test — the honest version*, and the
*One worker* paragraph under *Production mapping* (which will be wrong once D2 lands). The README currently
states the 1-worker numbers as the headline.

---

## 5. Watch-outs

1. **`docker compose run` silently discards a `-f` override.** `docker compose --profile load run --rm k6`
   has `depends_on: app`, so it **recreates `app` from the base compose file**, throwing away any
   `-f override.yml` applied earlier. This produced a completely wrong measurement in this session
   ("8 workers changes nothing" — it was 1 worker both times). When testing a non-default app config, run k6
   with `docker run --rm --network ema-assignment_default -e BASE_URL=http://app:8000 ...` and verify with
   `docker compose exec -T app sh -c 'tr "\0" " " < /proc/1/cmdline'` **before** trusting any number.
2. **`ps` does not exist in the app image** (python:3.11-slim). Use `/proc`.
3. **Always warm the cache in k6 `setup()`** before a steady-state measurement, or the first seconds are a
   thundering herd of live fetches and the percentiles describe cold start.
4. **`OTEL_EXPORTER=none` during load runs** — already handled in the `make load` target, which also restores
   the file exporter afterwards because `make trace` needs it.
5. `MOCK_LATENCY_SCALE=1.0` in compose means live fetches cost 40ms (GitHub) / 180ms (Jira). Cache hits stay
   ~0.4ms. Any hit-ratio scenario is really choosing a blend of those two.
6. **LAW 1 bites constantly in this repo.** Phase 4 needed four unplanned test-file splits, one of which
   (`test_federation.py` at 505 lines) would have been blocked by the commit hook. Check `wc -l` as you go.
7. `tests/unit/conftest.py` is at **398 lines**, two under the decompose threshold. The next fixture added
   to it should trigger a split.

## 6. Open questions

1. **Pool bound and idle TTL** — 32 instances / 10 min are suggestions, not measurements. Size them against
   actual per-instance memory (measure it) × worker count.
2. **Spill key derivation** — HKDF vs domain-separated hash. Either is defensible; pick one and write the
   reasoning down.
3. **Does the per-tenant pool change the answer for `/v1/query` under many tenants?** The capacity curve in
   §1.3 was measured with ONE tenant, so it never exercised eviction. T506's distribution scenario is the
   first time that path sees load — expect it to be the interesting result.
4. **Is `join_sources` still worth a thread** once it drops to ~1.5ms? `asyncio.to_thread` has its own
   overhead (~50–100µs). Re-measure after T502; it may be better inline, which would also simplify the span.

## 7. Context you should not have to rediscover

- **What the brief actually asks.** Line 160 asks for a *script that reaches* 500–1k QPS ("local acceptable"),
  not a laptop that serves it. Line 120 asks for **sizing math** for 1k QPS — a design-doc deliverable.
  Line 35's SLO (P50 < 500ms, P95 < 1.5s) is explicitly scoped to **single-source predicate-pushdown
  queries**, not cross-app joins. Line 142 puts "1k QPS synthetic test" at **month 4 of six** with 6.5
  engineers. The rubric has no throughput line: Architecture & Trade-offs 30%, Prototype Quality 15%.
- **Connector quota, not connector latency, is the real production ceiling.** Connector latency is `await`
  — wall-clock, not CPU — so it sets concurrency (Little's Law: 500 RPS × 220ms = 110 in flight), never
  throughput. But GitHub REST allows 5,000 req/hour/token = **1.39 req/s**. At 1k QPS across 100 tenants the
  required cache hit ratio is `1 − 139/1000` ≈ **86%**; for a single tenant driving 1k QPS it is **99.86%**.
  You cannot serve 1k QPS of *fresh* data from a quota-limited SaaS API — which is precisely why
  `max_staleness_ms`, the freshness cache and ETag revalidation exist. This belongs in the design doc's
  Capacity section (brief line 120) and is the strongest argument the prototype's architecture makes.
- **Head-of-line blocking** (brief line 121) is still unaddressed: there are no per-connector concurrency
  pools, so a queue of slow Jira calls can starve GitHub queries of workers. The only bound today is the
  per-source deadline. Worth one paragraph even if not built.

## 8. How to reproduce every number here

Probe scripts were run inside the app container and deleted afterwards; they are not in the repo. The pattern:

```bash
docker compose cp /tmp/probe.py app:/app/probe.py
docker compose exec -T app python probe.py
docker compose exec -T app rm -f /app/probe.py
```

The k6 sweep harness lives in this session's scratchpad only. Re-create it as a committed file under `load/`
as part of T506 — the capacity curve is a submission-grade artifact and should be reproducible by the
reviewer, not just by us.
