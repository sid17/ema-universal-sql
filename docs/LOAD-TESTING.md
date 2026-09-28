# How to Load Test This System

> The *method*. The numbers are in **[LOAD-RESULTS.md](LOAD-RESULTS.md)**; what is still
> outstanding is in **[LOAD-NEXT-STEPS.md](LOAD-NEXT-STEPS.md)**.

---

## 1. Assumptions

| | Value |
|---|---|
| Workers | **8** uvicorn processes on a 12-core laptop |
| Offered load | **500 QPS for 60s**, held constant across every scenario |
| Connector latency (simulated) | GitHub **40ms**, Jira **180ms** — fetched in parallel, so a miss has a ~180ms floor |
| Connector quota | GitHub **5,000 req/hr per tenant** = **1.39 req/s** |
| Tenants | **20** |
| Dataset | **200 rows per source** after pushdown, `LIMIT 50` out |
| Latency SLO | P50 < 500ms, P95 < 1.5s (brief line 35) |

A 10s warm-up runs before each scenario and is excluded from the reported numbers.

---

## 2. Where the time goes

Per request, warm cache, 2-source join:

| | Cost |
|---|---|
| **DuckDB join** (pooled) | **~2ms** |
| SQL parse + entitlement + plan | ~1.3ms |
| Audit INSERT + envelope assembly | ~0.7ms |
| **Total, cache hit** | **~5ms** |
| **Total, cache miss** | **~200ms** — Jira's round trip, nothing else |

**A miss costs 40× a hit.** That single ratio is why everything below is about the cache.

---

## 3. The two things that decide throughput

### 3.1 Cache hit ratio — because the connector quota is the real ceiling

A connector's quota is **per tenant**. So the hit ratio the system must sustain is not a
preference, it is arithmetic:

```
h  ≥  1 − (tenants × 1.39 req/s) / QPS
```

| Tenants | Required hit ratio @ 500 QPS | @ 1k QPS |
|---|---|---|
| 1 | 99.7% | 99.9% |
| **20** | **94.4%** | 97.2% |
| 100 | 72.2% | 86.1% |

At 20 tenants we target **95%**. The connector is not the bottleneck — the hit ratio required to
stay inside its quota is. (Brief line 120 asks for exactly this sizing math.)

### 3.2 DuckDB join vs. an in-process join

We measured a hand-written Python hash join against DuckDB. Python was **13–96× faster** at every
cardinality tested. We kept DuckDB anyway:

- The hand-written join **silently disagreed** with DuckDB on `ORDER BY` ties at 2,000 rows — same
  27 rows, different order. It passed at 20 and 200 rows. A divergence that appears only at certain
  data volumes is exactly what a size-based router would produce.
- DuckDB executes the **entitled** tree — the RLS predicate and CLS mask are compiled into that
  AST. Two evaluators means the entitlement filter can evaluate differently depending on result
  size.
- After pooling, the join is ~2ms of a ~5ms request against a **500ms** P50 budget. The saving
  would be 0.4% of the SLO.

Revisit if a profile ever shows the join as the constraint. It currently isn't.

---

## 4. How the DuckDB path was optimised

- **One instance per tenant, pooled** — was `duckdb.connect(":memory:")` per request at **6.5ms**;
  a cursor off a live instance is **0.01ms**. Bounded LRU, ~2.5 MiB per instance.
- **A cursor per request, not a connection** — `register()` is cursor-local, so concurrent requests
  never see each other's tables and registrations die with the cursor.
- **`threads=1`** — DuckDB defaults to one scheduler thread per core, *per instance*. With an
  instance per tenant that multiplies into hundreds of threads for no gain on 200-row inputs.
- **`temp_directory=''`** — the default spills tenant rows to disk **in plaintext** under memory
  pressure. Disabled, with a per-tenant `memory_limit`, so "too big" fails loudly instead.
- **Join runs off the event loop** (`asyncio.to_thread`) — otherwise every concurrent request
  serialises behind one core.

Net: the federation stage went from **13.7–18.0ms → 3.1–5.1ms**.

---

## 5. How the data is generated

Load tenants get their own synthetic dataset (`src/connectors/synthetic.py`); the committed 20-row
fixtures are left untouched so no existing test changes meaning.

- **Per-tenant rows, stamped.** Every row carries its `tenant_id` in `title` — a *projected*
  column. k6 checks every row of every response for a foreign stamp, so tenant isolation is
  asserted under concurrency rather than only in a unit test.
- **A key space.** The cache key is built from the pushed-down predicates, so the WHERE-clause
  literals *are* the key space. Rows span 20 repos (GitHub) and 20 projects (Jira), and both
  predicates move together — with only one varying, the other source would hit every time and the
  measured ratio would describe half the system.
- **Zipf draw, not uniform.** Real key popularity is heavy-tailed. A uniform draw gives every cache
  entry identical pressure, which is the one distribution production never has.
- **Misses come from the freshness knob.** A cold key misses once and is warm forever after — over
  30,000 requests that is noise, not a 5% miss rate. In production a miss happens because a caller
  *needs fresh data*, so 5% of requests ask for `max_staleness_ms: 0` and take the live path.
- **The hit ratio is measured, never asserted.** Every response carries `sources[].served`; k6 feeds
  a `Rate` from it and reports achieved next to intended.

---

## 6. Scenarios

All four at **500 QPS, 60s, 8 workers**. Only the marked variable changes.

| | Scenario | Variable | Isolates | Why |
|---|---|---|---|---|
| **S1** | Single source, 100% hit | no join | parse + entitlement + plan + 1 Redis GET + 1-table DuckDB | The floor for a real query |
| **S2** | 2-source join, 100% hit | join on | **the DuckDB join, alone** | The headline "can the engine do 500" |
| **S3** | 2-source join, ~95% hit, 20 tenants | realistic mix | production shape | The number that goes in the README |
| **S4** | 2-source join, 0% hit | cold cache | connector + rate limiter | **Expected to fail** — see below |

**S4 is supposed to fail.** At a 0% hit ratio, 500 QPS demands 500 live GitHub fetches/second
against a 1.39 req/s per-tenant quota. The system cannot serve that and must not pretend to. What
S4 shows is that it fails *correctly*: 429 with `Retry-After` and an async-reroute hint, the served
200s still inside their SLO, nothing melting.

**Method, in one line:** open model (`constant-arrival-rate`), offered rate held constant and
*achieved* rate reported, `dropped_iterations` always printed, thresholds never tuned to pass.

---

## 7. Running it

```bash
make up            # postgres, redis, app (8 workers)
make seed          # connectors, grants, policies, budgets
make load-seed     # the 20 synthetic load tenants

make load-mt                                          # S3 — the realistic scenario
make load-mt MISS_RATE=0                              # S2 — 100% hit, the join alone
make load-mt MISS_RATE=1.0 LOAD_MAX_REQUESTS=84       # S4 — cold cache, quota-bound
make load-mt RATE=750                                 # a different offered rate
make load-mt SYNTHETIC_ROWS=2000                      # a different per-source cardinality
```

**`LOAD_MAX_REQUESTS` is what makes S4 mean anything.** Load tenants are seeded with a deliberately
generous budget (5,000/60s) so S1–S3 measure the *engine* rather than the token bucket. `84` is
GitHub's real quota — 5,000/hour — and only with it does the rate limiter actually bind. Burst is
derived as one tenth of the budget; a fixed burst of 500 on an 84-request quota lets the first 584
calls through and the ceiling never appears.

`make load-mt` rebuilds the image, sets `OTEL_EXPORTER=none` (span export distorts the latency being
measured), seeds the tenants, flushes the cache so every run starts from a known state, runs k6 from
its own container, and restores the defaults afterwards. Two guards, both of which exist because
they were needed:

- It **verifies the app actually got the configuration** before measuring. `docker compose run`
  honours `depends_on` and silently recreates `app` from the base compose file, discarding anything
  set only on the `up` line — that produced one completely wrong measurement.
- It **always rebuilds**. `src/` and `scripts/` are copied into the image, not bind-mounted, so
  without `--build` a run measures whatever was baked in last time.

The k6 script has zero remote imports, so it works offline.

### Artifacts

| Path | From |
|---|---|
| `docs/artifacts/load/` | `make load-mt` — per-scenario throughput, latency, measured hit ratio, leak count |
| `docs/artifacts/trace/` | `make trace` — one request's stage waterfall |
| `docs/artifacts/metrics/` | `make scrape` — `GET /metrics` with real samples |
| `docs/artifacts/demo/` | `make demo` — the scripted walkthrough |

---

## 8. What this deliberately does not do

- **One load generator, one host.** No distributed generation; the honest ceiling is one laptop's.
- **Mocked connectors.** Latency and quota are simulated faithfully; a real API's tail behaviour,
  throttling headers and pagination cost at scale are not.
- **60s, not a soak.** Memory growth, cache eviction under pressure and pool exhaustion are not
  exercised.
- **Fixed 8 workers.** No autoscaling — the brief places that at M4.
