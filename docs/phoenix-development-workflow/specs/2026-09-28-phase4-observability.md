# Phase 4 — Observability, Load & Artifacts — Design Spec

> **Source of truth:** [`docs/design/phases/phase-4-observability.md`](../../design/phases/phase-4-observability.md),
> with seven corrections applied 2026-09-28 before this spec was written. The phase file wins on any conflict.
> ADRs 037–045: [`docs/kickoff/v5/architecture.md`](../../kickoff/v5/architecture.md). Measurements:
> [`docs/kickoff/v5/research-repos.md`](../../kickoff/v5/research-repos.md).
>
> **This spec adds what the phase file does not carry:** MUST/SHOULD/COULD tiering per deliverable
> (`02-DEFINITION-OF-DONE.md` §3), explicit file paths, the test list as named files, and the README sections
> this phase fills.

---

## Context

Phases 0–2 shipped a working federated query engine. Phase 4 makes its performance **legible** and packages the
submission. It holds four MUST-tier deliverables (k6, the Prometheus metric, the trace, the README) and depends
only on Phase 2, which is why it runs before Phase 3.

Because `03-BUILD-PROCESS.md` front-loaded observability, most of the instrumentation already exists: the OTel
provider, the `stage_span` decorator, `current_trace_id()`, the `/metrics` route, the JSONL span sink, and five
stage spans written by the person who wrote each stage. **Phase 4 is therefore smaller than its file suggests —
and larger in one specific way**, because the probe pass found four gaps between a locked document and the code
(v5 F3–F6). Closing those is most of the work.

### What exists vs what this phase builds

| | Exists (P0/P2) | Phase 4 builds |
|---|---|---|
| Spans | `gateway`, `parse`, `entitlement`, `plan`, `federation`, `assemble` | `connector.github`, `connector.jira`, `duckdb_join` |
| Metrics | golden signals, `rate_limit_remaining` **declared** | `query_duration_seconds`, `connector_fetch_duration_seconds{connector}`, gauge **fed** |
| Mocks | deterministic data, pagination, forced 429/timeout | simulated latency (HLD line 39, never built) |
| Load | — | `load/query_load.js`, compose `k6` profile, `make load` |
| Artifacts | `docs/demo-output.txt` | `trace-waterfall.txt`/`.png`, `k6-summary.txt`, `metrics-scrape.txt` |
| README | quickstart, what-it-proves, trade-offs, secrets | production mapping (rest), screenshots, repo access |

---

## Architecture

```
src/observability/
├── tracing.py          (exists — unchanged)
├── metrics.py          + query_duration_seconds, connector_fetch_duration_seconds,
│                         observe_query(), observe_connector_fetch(), set_rate_limit_remaining()
└── logging.py          (exists — unchanged)

src/execution/
├── federation.py       + connector.{type} span, duckdb_join span, histogram observation   [ADR-037/040]
└── assemble.py         + gauge feed in _budgets                                           [ADR-043]

src/connectors/
├── base.py             + simulated_latency_ms hook
├── mock_adapter.py     + await sleep on the LIVE path only                                [ADR-038]
├── github.py / jira.py + simulated_latency_ms = 40 / 180

src/gateway/routes.py   + query_duration_seconds observation around the pipeline call      [ADR-040]
src/config.py           + MOCK_LATENCY_SCALE (default 0.0)

scripts/waterfall.py    NEW — stdlib only, spans.jsonl -> ASCII + SVG                      [ADR-041/045]
load/query_load.js      NEW — k6, zero remote imports                                      [ADR-039]
docker-compose.yml      + k6 service under `profiles: [load]`, + MOCK_LATENCY_SCALE
Makefile                + real `load`, + `trace`, + `scrape`, + `artifacts`
```

### Data flow — the three views of one number

The load-bearing property of this phase's instrumentation is that per-connector timing is measured **once** and
surfaces three ways, so they cannot disagree:

```
SourceFetch.elapsed_ms  (federation.py, one perf_counter pair)
   ├─> span "connector.jira" attribute stage.elapsed_ms   -> traces/spans.jsonl -> waterfall
   ├─> connector_fetch_duration_seconds{connector="jira"}  -> /metrics -> P50/P95
   └─> envelope stats.connector_ms["jira"]                 -> the API response
```

A reviewer who cross-checks the trace against the envelope against the scrape finds the same figure. That is
the point of instrumenting where the work happens (ADR-037) rather than synthesizing spans afterwards.

---

## Tech stack + key decisions

| Decision | Pick | Why | ADR |
|---|---|---|---|
| Missing spans | inline `start_as_current_span` inside `FederationEngine` | `stage_span` takes a static name; both fetches would collapse to one label. Inside the `gather` closure, OTel context makes the parallel overlap visible | 037 |
| Mock latency | per-adapter constant, live path only, scale defaults 0.0 | Closes HLD line 39. Live path keeps cache hits fast (ADR-023/024 intact) and k6 measuring the engine | 038 |
| k6 imports | `k6/http` + `k6` only; summary rendered in-script | jslib's `textSummary` is a remote import — `make load` would fail offline and couple an artifact to a CDN | 039 |
| Metric names | add the two missing under the stated names; keep instrumentator labels | Renaming `{handler,method,status}` means replacing the library's collectors for a spelling | 040 |
| Trace artifact | stdlib renderer → `.txt` (reproducible) + one-time `.png` | Honours ADR-016's no-backend promise; no new dependency; text diffs and greps | 041 |
| Audit batching | **measure first**, build only if it shows | LAW 5; this is the phase whose deliverable is evidence | 042 |
| Gauge feed | `ResultAssembler._budgets` | Already computes `remaining` per connector per query; envelope and metric become the same number | 043 |
| Load target | 500 RPS as the brief names, thresholds untuned | A threshold rewritten until green measures patience, not the system | 044 |
| Span log | rotated by `make trace` | 34 MB / 9,554 traces measured; a stale trace would describe old code | 045 |

---

## Data contracts

### Span record (measured, `traces/spans.jsonl`, 2026-09-28)

```json
{"name": "federation",
 "context": {"trace_id": "0xfd16a71d…", "span_id": "0x…", "trace_state": "[]"},
 "kind": "SpanKind.INTERNAL", "parent_id": "0x…",
 "start_time": "2026-09-28T08:39:35.869308Z", "end_time": "2026-09-28T08:39:35.884Z",
 "status": {"status_code": "UNSET"},
 "attributes": {"stage.elapsed_ms": 15.51}}
```
Verified: `python -c` over the file — 27 distinct names, 9,554 traces, every record carries `parent_id`.
**Caveat (F2):** `BatchSpanProcessor` drops whatever is queued at exit, so per-name counts are uneven
(1135 `parse` vs 1082 `gateway`). The renderer must select a **complete** trace.

### k6 `handleSummary` payload (measured, k6 v2.3.0, 2026-09-28)

```
metrics: checks,data_received,data_sent,iteration_duration,iterations,vus,vus_max
data.metrics.iteration_duration.values =
  {"min":0.019583,"med":0.070708,"max":0.35675,"p(90)":0.131,"p(95)":0.14825,"avg":0.0794851}
data.state = {"isStdOutTTY":false,"isStdErrTTY":false,"testRunDurationMs":3002.019945}
```
Verified: `docker run --rm -v $PWD:/out grafana/k6:latest run /out/probe.js`, and the returned path was
written through the bind mount. Percentiles are present without any jslib import.

### Envelope stats (measured, live canonical query, 2026-09-28)

```json
{"parse_ms": 1.51, "entitlement_ms": 0.06, "plan_ms": 0.12, "federation_ms": 15.51,
 "connector_ms": {"github": 2.05, "jira": 2.05}, "rows_examined": 3, "assemble_ms": 0.59}
```
This is the **pre-latency baseline**. After ADR-038 the same query should show `connector_ms` around
`{github: ~42, jira: ~182}` on a live fetch and unchanged on a cache hit — which is the assertion that proves
the latency landed on the right side of the cache check.

### `/metrics` scrape (measured, 2026-09-28)

```
# HELP rate_limit_remaining Requests left in the tenant's token bucket for a connector.
# TYPE rate_limit_remaining gauge
                       <- no samples. `assert "rate_limit_remaining" in body` passes on the HELP line.
```

---

## User flows

### Flow 1 — a reviewer reads where the time went
`make up && make seed` → `make trace` → `docs/trace-waterfall.txt`. The renderer truncates the span log, runs
one canonical query, reads back the trace by id, and prints seven proportional bars. Expected shape: `jira`
visibly the widest, overlapping `github`, with the engine stages hairlines. **That overlap is the evidence the
federation is parallel**, and the width contrast is the evidence that the bottleneck is the source, not us.

### Flow 2 — a reviewer measures the engine under load
`make load` → compose starts the `k6` profile service on the compose network → 60s at 500 RPS against
`tenant_load` with `max_staleness_ms: 60000` (cache hits dominate) → `handleSummary` writes
`docs/k6-summary.txt` with p(50)/p(95), achieved-vs-requested RPS, `dropped_iterations`, and the drain
scenario's 429 check. Then `make scrape` captures `/metrics` with real histogram buckets in it.

### Flow 3 — the honest-degradation story stays true under instrumentation
`POST /v1/test/fail-next {"connector":"jira"}` → canonical query → the `connector.jira` span still **closes**
(it wraps the `asyncio.wait_for` that raised), carries the timeout duration, and the envelope is `partial`.
A span that leaks on the error path would make the waterfall's worst-case reading the least trustworthy one.

---

## Features by phase-task, with tier

| Deliverable | Tier | DoD reference |
|---|---|---|
| `connector.*` + `duckdb_join` spans | **MUST** | §3 "1 trace showing connector time" (brief 161) |
| `query_duration_seconds`, `connector_fetch_duration_seconds` | **MUST** | §3 "1 Prometheus metric" (brief 161) |
| `rate_limit_remaining` fed + its test tightened | **MUST** | phase file acceptance gate |
| `load/query_load.js` + `make load` + `docs/k6-summary.txt` | **MUST** | §3 "k6 ~500–1k QPS for 60s" (brief 160) |
| `docs/trace-waterfall.txt` + `.png` | **MUST** | §1 gate 5 (brief 52/167) |
| README: production mapping, screenshots §, status | **MUST** | §1 gate 6 (brief 16) |
| Simulated connector latency | **MUST** (derived) | HLD line 39 — a locked claim currently false |
| `docs/metrics-scrape.txt` | SHOULD | phase file acceptance: "a `/metrics` scrape" |
| Audit batching **if measured to dominate** | SHOULD | phase file; ADR-042 gates it |
| Repo access grant + README URL | **MUST**, *deferred* | §1 gate 2 — **owner action, not this phase** |
| Cold-start re-timing < 60s | **MUST** | §1 gate 3 |

**Explicitly out of scope:** the Phase 3 console screenshot (bonus, not a gate — the brief asks for a *metrics
or trace* screenshot), `make e2e` / Playwright (correction 7), and anything on the COULD list.

---

## Test list (named files, for `/plan-phase` to checkbox)

### `tests/unit/` — hermetic, runs on the commit hook

| File | New/extended | Covers |
|---|---|---|
| `test_observability.py` | extended | the two new histograms register, observe, and render; `observe_query` records on the error path too |
| `test_federation.py` | extended | `connector.{type}` and `duckdb_join` spans emitted, correctly parented, and **still closed on timeout and on ApiError** |
| `test_connectors.py` | extended | latency applied on live fetch, **not** on a cache hit; `MOCK_LATENCY_SCALE=0` is a true no-op |
| `test_assemble.py` | extended | `_budgets` sets the gauge with both labels, and with the same value it puts in the envelope |
| `test_waterfall.py` | **new** | trace selection rejects an incomplete trace (LAW 4 loud failure); bar widths proportional; renders with one source timed out |

### `tests/integration/` — against the running stack

| File | New/extended | Covers |
|---|---|---|
| `test_metrics.py` | **new** | after a query: `rate_limit_remaining{connector=…,tenant=…} <n>` **as a labelled sample with a value**; both histograms have `_count > 0`; `query_duration_seconds` counts a 400 as well as a 200 |
| `test_trace.py` | **new** | a query's `trace_id` resolves to a span set in the JSONL containing all seven span names, `connector.*` parented to `federation` |
| `test_scaffold.py` | **corrected** | the tautological `assert "rate_limit_remaining" in body` replaced (correction 3) |
| `test_staleness_knob.py` | verify unchanged | the latency must not break the live→cache flip |

### Harness-level

| Check | How |
|---|---|
| `make load` on a fresh clone with no host k6 | run it; assert `docs/k6-summary.txt` is written and non-trivial |
| `make trace` reproducible | run twice; assert a valid waterfall both times |
| cold start < 60s | `make down -v && time make up` |

---

## README sections this phase fills

Per the README-growth table established in Phase 0. Three sections currently read *"Pending Phase 4."*:

| § | Content |
|---|---|
| Production mapping → **Everything else** | one paragraph per remaining HLD §7 non-goal: mock→live connector (the `fetch` seam), mock-JWT→OIDC, 429-fail-fast→async `202+job_id`, in-memory→spill-to-disk, per-user DRR fairness, residency enforcement, admin console, docker-compose→k8s/Helm/Terraform, audit durability, gauge cardinality |
| **Screenshots — and what they prove** | the waterfall (+ the "P95 was Jira not the engine" reading), the k6 summary, the `/metrics` scrape; each with the one sentence saying what it proves |
| **Repository access** | the grant steps for `souvik-sen@ema.co` / `careers@ema.co`. **URL left as a placeholder — owner action, deferred by explicit instruction.** |
| **Current status** (existing) | updated: Phase 4 complete, Phase 3 remaining |
| **Quickstart / Make targets** (existing) | `make load`, `make trace`, `make scrape` added |

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Latency lands on the wrong side of the cache check → breaks the staleness demo and makes k6 measure sleeps | Placement is asserted directly: a test that a cache hit costs ~0 with latency enabled. `test_staleness_knob.py` re-run as a regression |
| A span leaks on the timeout path, making the worst case the least trustworthy trace | `with` blocks, plus an explicit unit test on the timeout and ApiError paths |
| 500 RPS saturates the host and k6 measures itself | Report achieved-vs-requested RPS and `dropped_iterations` in the summary, so distortion is visible rather than hidden (ADR-044) |
| `federation.py` crosses LAW 1's 400-line decompose threshold (at 377) | Measured after the edit; if it crosses, the span/metric observation extracts to a small helper |
| Waterfall renders a batch-truncated trace and looks like an engine bug | Completeness check fails loudly with the missing span names (LAW 4), never renders a partial tree silently |
| The audit measurement says "batch it" late in the phase | ADR-042 makes it an explicit gate item in the plan, not an optional follow-up |
| Editing Phase 1 and Phase 2 modules re-opens reviewed code | Each edit is additive instrumentation plus one behavioural change (latency), each with its own test; the Phase 2 non-negotiables get a re-run, not a re-review |

## Source artifacts

- Phase file: `docs/design/phases/phase-4-observability.md` (7 corrections applied)
- ADRs: `docs/kickoff/v5/architecture.md` (037–045)
- Probes: `docs/kickoff/v5/research-repos.md` (F1–F6)
- Tiers: `docs/design/02-DEFINITION-OF-DONE.md` §1, §3
- Locked claims touched: HLD line 39 (simulated latency), HLD §9 (error vocabulary, unchanged)
