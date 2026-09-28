# Plan — Phase 4: Observability, Load & Artifacts

**Goal:** make performance legible and package the submission — seven-span waterfall, real Prometheus
histograms, a 500 RPS k6 run, and the three README sections still reading *"Pending Phase 4."*

**Depends on:** Phase 2 (complete, 13 commits, `c560a08`). Not on Phase 3.

**Assumes:** `src/models/` is frozen; the five pipeline stage spans and `stats.connector_ms` already exist
(front-loaded observability); `tenant_load` is seeded with a 5000/60s budget on both connectors; `TEST_MODE=1`
is available via `make test-mode`.

**Spec:** [`specs/2026-09-28-phase4-observability.md`](../specs/2026-09-28-phase4-observability.md)
**Phase file (wins on conflict):** [`../../design/phases/phase-4-observability.md`](../../design/phases/phase-4-observability.md) — 7 corrections applied
**ADRs:** [`../../kickoff/v5/architecture.md`](../../kickoff/v5/architecture.md) — 037–045

**Verify (the Iron Law — run it, read the output, then claim it works):**
```
.venv/bin/python -m pytest -q tests/unit          # all green, count > 550
make test-integration                              # all green, count > 89
make load                                          # writes docs/k6-summary.txt
make trace                                         # writes docs/trace-waterfall.txt
make scrape                                        # writes docs/metrics-scrape.txt
.venv/bin/ruff check src tests scripts             # clean
make down && time make up                          # < 60s cold
```

---

## File map

| File | Action | Task |
|---|---|---|
| `src/observability/metrics.py` | modify (+2 histograms, 3 observe fns) | T401 |
| `tests/unit/test_observability.py` | modify | T401 |
| `src/execution/federation.py` | modify (3 spans + histogram) | T402 |
| `src/execution/join.py` | **create, conditional** — only if `federation.py` crosses 400 | T402a |
| `tests/unit/test_federation.py` | modify | T402 |
| `src/execution/assemble.py` | modify (feed the gauge) | T403 |
| `tests/unit/test_assemble.py` | modify + **split** (already 401 lines) | T403 |
| `tests/unit/test_assemble_provenance.py` | create (the split half) | T403 |
| `src/gateway/routes.py` | modify (`query_duration_seconds`) | T404 |
| `src/config.py` | modify (`MOCK_LATENCY_SCALE`) | T405 |
| `src/connectors/base.py` | modify (`simulated_latency_ms` hook) | T405 |
| `src/connectors/mock_adapter.py` | modify (sleep on the live path) | T405 |
| `src/connectors/github.py`, `jira.py` | modify (40ms / 180ms) | T405 |
| `tests/unit/test_connectors.py` | modify | T405 |
| `docker-compose.yml` | modify (`MOCK_LATENCY_SCALE`, k6 profile service) | T405, T407 |
| `scripts/waterfall.py` | create | T406 |
| `tests/unit/test_waterfall.py` | create | T406 |
| `load/query_load.js` | create | T407 |
| `Makefile` | modify (`trace`, `load`, `scrape`, `artifacts`) | T406, T407, T408 |
| `tests/integration/test_metrics.py` | create | T408 |
| `tests/integration/test_trace.py` | create | T409 |
| `tests/integration/test_scaffold.py` | modify (kill the tautology) | T409 |
| `src/governance/audit.py` | modify — **conditional on T410's measurement** | T411 |
| `docs/k6-summary.txt`, `trace-waterfall.txt`, `trace-waterfall.png`, `metrics-scrape.txt` | create (artifacts) | T410, T412 |
| `README.md` | modify (3 pending sections + status + targets) | T413, T414 |
| `docs/design/03-BUILD-PROCESS.md` | modify (tracker tick) | T415 |

**No file is projected past 500.** Two are projected past LAW 1's 400-line decompose threshold and are handled
by explicit split tasks (T402a, T403), not by letting them drift.

---

## Milestone A — close the four instrumentation gaps (BLOCKING)

Everything downstream reads these. No artifact task starts until A is green.

### - [x] T401 — the two missing histograms and the three observation helpers

**Files:** `src/observability/metrics.py`, `tests/unit/test_observability.py`

**Decision:** add `query_duration_seconds` (no labels) and `connector_fetch_duration_seconds` (label:
`connector`) under exactly the names the phase file states, and expose `observe_query(seconds)`,
`observe_connector_fetch(connector, seconds)`, `set_rate_limit_remaining(tenant, connector, n)` — **because**
the call sites (`routes.py`, `federation.py`, `assemble.py`) should not each import `prometheus_client` and
reach for a global; one module owns the registry (ADR-015's shape, extended). Reuse `_get_or_create_gauge`'s
duplicate-tolerant pattern for the histograms — it already exists and solves the same re-import problem
(LAW 2).

**Steps:** declare both with the duplicate-tolerant helper generalized to `Histogram`; default buckets (top
bucket 10s comfortably covers the 5s request deadline); three thin observe functions.

**Verify:** `pytest -q tests/unit/test_observability.py` — new tests assert both render in
`generate_latest`, that `observe_*` moves `_count` and `_sum`, and that importing the module twice does not
raise.

### - [x] T402 — `connector.{type}` and `duckdb_join` spans, plus the connector histogram

**Files:** `src/execution/federation.py`, `tests/unit/test_federation.py`

**Decision:** open the span **inside** the per-source `run()` closure and around the DuckDB execution, using
`get_tracer().start_as_current_span` directly rather than `@stage_span` — **because** `stage_span` takes a
static name and both fetches would collapse into one indistinguishable label, and because a span opened inside
the `gather` closure inherits OTel context so the two fetches render as genuinely overlapping (ADR-037). The
overlap is the evidence that the federation is parallel.

**Steps:** wrap the `wait_for` in `connector.{connector_type}`; set `stage.elapsed_ms` from the *existing*
`perf_counter` pair (one measurement, three views — do not add a second timer); call
`observe_connector_fetch` on every exit path including timeout and `ApiError`; wrap `_join` in `duckdb_join`.
Skip both for a `yields_nothing` source — a span for a call never made would be a lie.

**Verify:** `pytest -q tests/unit/test_federation.py` with an `InMemorySpanExporter`. Assertions:
both connector spans present and parented to the active span; **the span still closes on the timeout path and
on the `ApiError` path**; `duckdb_join` present; no `connector.*` span for a default-denied source; the span's
`stage.elapsed_ms` equals `SourceFetch.elapsed_ms`.

### - [x] T402a — split the DuckDB join into `src/execution/join.py` **if** T402 pushes `federation.py` past 400

**Decision:** conditional, and the split is by responsibility, not by line count: `federation.py` keeps
fetch orchestration, `join.py` takes Arrow registration + SQL execution. `federation.py` is at **377**; T402
adds ~15–20. If the measured result is ≥400, split; if under, do not — LAW 5.

**Verify:** `wc -l src/execution/*.py` after T402; then the full unit suite unchanged.

### - [x] T403 — feed `rate_limit_remaining` from `_budgets`, and split the test file

**Files:** `src/execution/assemble.py`, `tests/unit/test_assemble.py`, `tests/unit/test_assemble_provenance.py`

**Decision:** set the gauge in `ResultAssembler._budgets`, in the same loop that already calls
`limiter.remaining()` for the envelope — **because** that makes the number `/metrics` publishes and the number
the caller sees provably the same value rather than two reads that can drift (ADR-043). Not in the limiter:
it deliberately takes no tenant-scoped reporting duty (ADR-020) and is skipped entirely on a cache hit, which
would leave the gauge stale.

**Also:** `tests/unit/test_assemble.py` is at **401 lines** — already past the decompose threshold before this
task adds anything. Split it now, by subject: rows/pagination/columns stay; sources/freshness/budgets/stats
move to `test_assemble_provenance.py`. This is a precondition of the task, not a follow-up (LAW 1).

**Verify:** `pytest -q tests/unit/test_assemble.py tests/unit/test_assemble_provenance.py`; new test asserts
`rate_limit_remaining.labels(...)._value` equals `envelope.rate_limit_status[c].remaining` for both connectors.
`wc -l` shows both files under 400.

### - [x] T404 — `query_duration_seconds` observed in the route, including the failure paths

**Files:** `src/gateway/routes.py`, `tests/unit/test_observability.py`

**Decision:** observe in the route around the `runner.run` call, not inside the runner — **because** a
histogram that only sees successes makes P95 look better than it is, and entitlement denials and 400s never
reach the runner at all (ADR-040). `try/finally`, so a raised `ApiError` is still counted.

**Steps:** a small `finally`-based timer; keep the handler under the 80-line route cap (currently ~30).

**Verify:** `pytest -q tests/unit` — a test asserting `query_duration_seconds_count` increments on a 200 **and**
on a raised `InvalidQueryError`.

### - [x] T405 — simulated connector latency, on the live path only

**Files:** `src/config.py`, `src/connectors/base.py`, `src/connectors/mock_adapter.py`,
`src/connectors/github.py`, `src/connectors/jira.py`, `docker-compose.yml`, `tests/unit/test_connectors.py`

**Decision:** a `simulated_latency_ms` class attribute — **GitHub 40, Jira 180** — applied with
`asyncio.sleep` *after* the cache check and *after* the token consume, scaled by `MOCK_LATENCY_SCALE`
(default **0.0**, set to `1.0` in compose). **Because** HLD line 39 lists simulated latency as built and it is
not (v5 F4), and because placement is the whole decision: before the cache check it would destroy the
staleness demo and make k6 measure sleeps, which the phase file explicitly forbids (ADR-038). Jira is the
slower one deliberately — it is the source carrying RLS and CLS, so "the entitled source is also the slow one"
is the shape the waterfall should show. Fixed, not jittered: Phase 1 chose deterministic datasets so tests
never flake, and a random sleep reintroduces that through the back door.

**Verify:** `pytest -q tests/unit/test_connectors.py` — latency applied on a live fetch; **not** applied on a
cache hit; `MOCK_LATENCY_SCALE=0.0` is an exact no-op (no `sleep` call at all, not `sleep(0)`); the full unit
suite runtime does not regress. Then `make test-integration` with `test_staleness_knob.py` as the regression
guard that live→cache still flips.

---

## Milestone B — the artifact producers

### - [x] T406 — `scripts/waterfall.py` and `make trace`

**Files:** `scripts/waterfall.py`, `tests/unit/test_waterfall.py`, `Makefile`

**Decision:** standard library only, reading `traces/spans.jsonl` — **because** ADR-016 declined a tracing
backend and ADR-041 keeps that promise; matplotlib would be a heavy dependency for one image and Jaeger would
be a fourth container against a <60s cold-start gate. Selects the newest **complete** trace (or `--trace-id`),
where complete means every expected span name present and every `parent_id` resolvable — and **fails loudly
naming the missing spans** rather than rendering a tree with holes (LAW 4), because `BatchSpanProcessor` drops
its queue at exit and a truncated trace would read as an engine bug (v5 F2).

**Steps:** parse → group by `trace_id` → completeness check → lay out on a shared axis → render proportional
bars + an SVG. `make trace` truncates the span log, runs one canonical query, then renders (ADR-045).

**Verify:** `pytest -q tests/unit/test_waterfall.py` against a fixture JSONL: rejects an incomplete trace with
the missing names in the message; bar widths proportional to duration; renders correctly when one source timed
out. Then `make trace` end to end and **read the output** — Jira must be visibly the widest bar.

### - [x] T407 — `load/query_load.js`, the compose k6 profile, and `make load`

**Files:** `load/query_load.js`, `docker-compose.yml`, `Makefile`

**Decision:** k6 as a `profiles: [load]` compose service on the compose network targeting `http://app:8000` —
**because** a profile never starts with `make up` (so it costs no cold-start time) and the compose network
avoids `host.docker.internal`, which does not exist on plain Linux (ADR-044). **Zero remote imports**: only
`k6/http` and `k6`, with `handleSummary` rendering the text itself from `data.metrics.*.values`, because the
idiomatic `jslib.k6.io` import would make `make load` fail offline and couple a graded artifact to a CDN
(ADR-039, measured in F1).

**Steps:** two scenarios — `constant-arrival-rate` 500 RPS / 60s against `tenant_load` with
`max_staleness_ms: 60000`, and a short drain scenario against `tenant_acme` checking a clean 429 with
`Retry-After` (checked, not a failure). Token minted in `setup()`, once. Summary reports p(50)/p(95),
**achieved vs requested RPS**, and `dropped_iterations`.

**Verify:** `make load` writes a non-trivial `docs/k6-summary.txt`; `docker images | grep k6` is the only new
requirement and it is pulled by compose. Confirm `make up` still starts exactly three services.

### - [x] T408 — `make scrape` and `tests/integration/test_metrics.py`

**Files:** `Makefile`, `tests/integration/test_metrics.py`

**Decision:** the integration test asserts a **labelled sample with a numeric value**, not a substring —
because `assert "rate_limit_remaining" in body` is satisfied by the `# HELP` line and passes against exactly
the broken state it was written to catch (v5 F5, phase-file correction 3).

**Verify:** `pytest -q tests/integration/test_metrics.py`. Assertions: after one query,
`rate_limit_remaining{connector="github",tenant="tenant_acme"}` parses to a number;
`connector_fetch_duration_seconds_count{connector="jira"} > 0`; `query_duration_seconds_count` increments on
a 400 as well as a 200. Then `make scrape` → `docs/metrics-scrape.txt`.

### - [x] T409 — `tests/integration/test_trace.py`, and kill the tautological assertion

**Files:** `tests/integration/test_trace.py`, `tests/integration/test_scaffold.py`

**Verify:** a query's returned `trace_id` resolves in `traces/spans.jsonl` to a span set containing all seven
names, with `connector.*` parented to `federation`; and `test_scaffold.py:163`'s substring assertion is
replaced. Run the whole integration suite.

---

## Milestone C — measure, then decide (ADR-042)

### - [x] T410 — run the load test and **read the result** before touching `audit.py`

**Artifacts:** `docs/k6-summary.txt`

**Decision:** this is the gate ADR-042 turns on, made an explicit task so it cannot become an optional
follow-up. Run `make load` at the brief's 500 RPS. Then read the waterfall and the histograms and answer one
question in writing: **does the synchronous audit INSERT appear in the P95?**

**Verify:** `docs/k6-summary.txt` exists with p(50)/p(95), achieved-vs-requested RPS and `dropped_iterations`.
Record the answer in `REVIEW.md` either way. **Thresholds are not tuned to make this green** — a blown p(95)
is a finding, not a failure (ADR-044).

### - [x] T411 — **conditional:** batch the audit write, only if T410 showed it dominating

**Files:** `src/governance/audit.py`, `tests/unit/test_audit.py`

**Decision:** if and only if T410 says yes, build the phase file's fix exactly as specified — bounded
`asyncio.Queue`, background drain task, **drop-oldest with a counter that is surfaced, never a silent
discard** (LAW 4) — and re-run `make load` for a before/after number. If T410 says no, this task closes as
NOT DONE with the measurement as the reason, and the README reports the measured figure.

**Verify:** either the before/after pair in `docs/k6-summary.txt`, or the recorded measurement showing why the
change was not made.

### - [x] T412 — capture the artifacts

**Artifacts:** `docs/trace-waterfall.txt`, `docs/trace-waterfall.png`, `docs/metrics-scrape.txt`

**Steps:** `make artifacts` regenerates the reproducible three; the `.png` is a one-time capture of the SVG
(ADR-041). Confirm `docs/demo-output.txt` from Phase 2 is still current against the latency change —
**it will have changed**, so regenerate it with `make demo`.

**Verify:** every file in the DoD §1 gate-5 list exists and is non-empty; `docs/demo-output.txt` reflects the
current code.

---

## Milestone D — the README and the gate

### - [x] T413 — README: *Production mapping → Everything else*

One paragraph per remaining HLD §7 non-goal: mock→live connector (the `fetch` seam), mock-JWT→OIDC,
429-fail-fast→async `202 + job_id`, in-memory→spill-to-disk, per-user DRR fairness, residency enforcement,
admin console, docker-compose→k8s/Helm/Terraform, audit durability, and gauge label cardinality.

**Decision:** each paragraph names *where the full design covers it* — that is what keeps a scoped-down
prototype reading as deliberate rather than unfinished (DoD §3 cut-order rule).

**Verify:** every non-goal in HLD §7 has a paragraph; grep for `Pending Phase 4` returns one fewer hit.

### - [x] T414 — README: *Screenshots*, *Repository access*, status, and the new make targets

**Decision:** the Screenshots section states, per artifact, **what it proves** — the waterfall's reading is
"P95 was Jira, not the engine," and it is only honest to write that after T410/T412 produce the numbers, which
is why this task is last. **Repository access:** the grant steps for `souvik-sen@ema.co` / `careers@ema.co`
with the URL left as an explicit placeholder — deferred by the owner's instruction, and flagged in `REVIEW.md`
as the one open submission-gate item.

**Verify:** `grep -c "Pending Phase 4" README.md` → 0. Read the README end to end once, out loud (DoD §1
gate 6).

### - [x] T415 — cold-start re-timing and the tracker tick

**Files:** `docs/design/03-BUILD-PROCESS.md`

**Verify:** `make down && time make up` < 60s with the k6 profile present but not started; tick Phase 4's row.

---

## Verification Gate

The phase is not done until every line below passes, regardless of checkbox state.

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | Unit suite green | `.venv/bin/python -m pytest -q tests/unit` | > 550 passed, 0 failed |
| 2 | Integration green | `make test-integration` | > 89 passed, 0 failed |
| 3 | Lint clean, no config weakening | `.venv/bin/ruff check src tests scripts` | clean; no new `noqa`, no ignore list (LAW 7) |
| 4 | LAW 1 | `find src tests scripts load -type f \| xargs wc -l \| sort -rn \| head` | nothing ≥ 500; nothing ≥ 400 without a split task |
| 5 | **Seven spans** | `make trace` then read `docs/trace-waterfall.txt` | all seven present; `connector.jira` visibly widest; the two connector bars **overlap** |
| 6 | **Gauge has a sample** | `make scrape && grep '^rate_limit_remaining{' docs/metrics-scrape.txt` | a labelled line with a number — not a `# HELP` line |
| 7 | Load run | `make load` | `docs/k6-summary.txt` with p(50)/p(95), achieved-vs-requested RPS, `dropped_iterations`, and the drain scenario's 429 check |
| 8 | ADR-042 answered | `REVIEW.md` | states in writing whether the audit INSERT was in the P95, with the number |
| 9 | Latency is on the right side of the cache | `pytest -q tests/integration/test_staleness_knob.py` | live→cache still flips; a cache hit is still ~0ms |
| 10 | **Phase 2 non-negotiables still hold** | `pytest -q tests/integration/test_trichotomy.py tests/integration/test_cls.py tests/integration/test_timeout_partial.py` | green — instrumentation must not have changed behaviour |
| 11 | Cold start | `make down && time make up` | < 60s |
| 12 | README complete | `grep -c "Pending Phase 4" README.md` | 0 |
| 13 | Artifacts present | `ls -la docs/*.txt docs/*.png` | `demo-output`, `k6-summary`, `trace-waterfall.txt`, `trace-waterfall.png`, `metrics-scrape` all non-empty |
| 14 | End-to-end flow | fresh `make up && make seed && make demo && make trace && make load` | every step succeeds from a clean volume |

**Gate note on `ruff format`:** carried from Phase 2 — `ruff format --check` fails on 13 files at HEAD and
always has. The repo's enforced standard is `ruff check` (lint). Not adopted as a gate here; still flagged as
a standing decision.

**Known out of scope, stated so it is not mistaken for an oversight:** the Phase 3 console screenshot (bonus,
not a gate), `make e2e` / Playwright (correction 7 — carried to Phase 3), the repo URL and access grant
(owner action, deferred by instruction), and everything on the COULD list.
