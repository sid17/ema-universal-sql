# Phase 4 — Observability, Load & Artifacts

> **Goal:** make performance legible and package the submission. **Gate:** a trace waterfall that shows where time
> went, a k6 run at ~500 QPS, and a README that lets a reviewer `docker-compose up` in < 60s.
> **Depends on Phase 2 only — not on Phase 3.** Four of this phase's deliverables (k6, the Prometheus metric, the
> trace, the README) are MUST-tier while Phase 3 is SHOULD-tier, so **if time is tight, run this phase before
> Phase 3.**

> **Corrections applied 2026-09-28, before the Phase 4 spec was written.** Seven things below were
> written before Phases 0-2 existed, or before the runtime behaviour was measured, and are wrong against
> what was actually built. Fixed here rather than forked into the spec, per `03-BUILD-PROCESS.md` step 3.
> ADRs in [`../../kickoff/v5/architecture.md`](../../kickoff/v5/architecture.md); the measurements in
> [`../../kickoff/v5/research-repos.md`](../../kickoff/v5/research-repos.md).
>
> 1. **Only five of the seven spans exist** (ADR-037). Measured: `grep stage_span src/execution/` returns
>    nothing. `runner.py` spans the five pipeline stages; the per-connector fetches and the DuckDB join are
>    inside one opaque `federation` span. The "Done when" line — *read the trace to see "P95 was Jira not
>    the engine"* — is unreadable off the current span set. This phase adds `connector.github`,
>    `connector.jira` and `duckdb_join` **inside** `FederationEngine`, where the concurrency is visible.
> 2. **The mock connectors have no simulated latency** (ADR-038), though HLD line 39 lists it as built and
>    this file's own k6 section says the run "should not be measuring the mock's simulated latency."
>    Measured: `connector_ms: {github: 2.05, jira: 2.05}`. With both sources at 2ms the waterfall's honest
>    reading is *"the connectors are free, DuckDB is the cost"* — the inverse of the intended story. This
>    phase adds it, on the **live path only** so cache hits stay fast and k6 still measures the engine.
> 3. **`rate_limit_remaining` is declared and never fed** (ADR-043) — and the test guarding it cannot fail.
>    Measured: `/metrics` emits its `# HELP` and `# TYPE` lines and no samples, while
>    `test_scaffold.py:163` asserts `"rate_limit_remaining" in body`, which the `# HELP` line satisfies.
>    The acceptance gate below is currently met by a comment. Fed from `ResultAssembler._budgets`, and the
>    assertion is tightened to require a labelled sample with a value.
> 4. **`http_requests_total` is labelled `{handler, method, status}`, not `{route, code}`** (ADR-040).
>    Those are `prometheus-fastapi-instrumentator`'s defaults. Matching this file's spelling would mean
>    replacing the library's collectors to rename two labels — cost with no behavioural gain. The file is
>    corrected; the two genuinely missing metrics (`query_duration_seconds`,
>    `connector_fetch_duration_seconds`) are added under exactly the names stated.
> 5. **"Batch the audit write before this runs" is deferred behind a measurement** (ADR-042) — the one
>    deliberate deviation from this file in the phase. The claim that the INSERT *is* the P95 at 500 QPS is
>    plausible and checkable in four minutes, and this is the phase whose entire deliverable is evidence.
>    k6 runs against the synchronous INSERT first; if it dominates, the bounded-queue fix ships as specified
>    here, with a before/after number. LAW 5 forbids building it on a prediction.
> 6. **"Ramp to ~500 VUs/RPS" conflates two different things** (ADR-044). VUs are a concurrency pool; RPS is
>    an arrival rate. `constant-arrival-rate` targets the *rate* and allocates VUs to sustain it, which is
>    what the brief's "~500 QPS" asks for. Thresholds are **not** tuned to pass, and `dropped_iterations` is
>    reported — under-delivered load can otherwise report a flattering p(95).
> 7. **`make e2e` + `playwright install` is Phase 3's, not this phase's.** It is listed under
>    "Fresh-clone prerequisites" below, but `make e2e` is still a stub and Phase 3 has not run. Phase 4
>    verifies the two prerequisites it owns (`make load` needs no host k6; `make up` needs no host Python)
>    and carries the Playwright one forward to Phase 3's gate.
>
> **Also settled here:** DoD §1 gate 5 names `docs/trace-waterfall.png` while this file calls a
> structured-log waterfall acceptable. Both ship (ADR-041): `make trace` reproduces
> `docs/trace-waterfall.txt` from the span log with no backend and no new dependency, and the `.png` is a
> one-time capture of the same data so the gate's literal wording is met.

## Deliverables
1. OpenTelemetry spans across the pipeline + `trace_id` propagation.
2. `/metrics` Prometheus endpoint (golden signals + per-connector gauge).
3. `load/` — a k6 script (~500 QPS / 60s).
4. `README.md` — quickstart + trade-offs + prod-mapping of every non-goal.
5. Captured artifacts → `docs/` (trace waterfall, k6 summary, `/metrics` scrape).

## Observability (`src/observability/`)
- Wrap each stage in an OTel span; span names → the envelope's `stats.connector_ms` and a full waterfall:
  `gateway`, `parse`, `entitlement`, `plan`, `connector.github`, `connector.jira`, `duckdb_join`. The root
  span's id = the envelope `trace_id`.
  **[correction 1]** The first four plus `federation`/`assemble` exist (Phase 0/2, front-loaded). The last
  three do **not** and are this phase's work, inside `FederationEngine` — see ADR-037. Span names are the
  bare stage name; the `_ms` suffix belongs to the `stats` keys and to `stage.elapsed_ms`, not to the span.
- Export to console/OTLP; for the screenshot, a local Jaeger/Tempo container is optional — a structured-log waterfall is acceptable per the take-home.
- `/metrics` (prometheus-client): `http_requests_total`, `query_duration_seconds` histogram (→ P50/P95),
  `connector_fetch_duration_seconds{connector}`, and a `rate_limit_remaining{tenant,connector}` gauge.
  **[correction 4]** `http_requests_total` already exists, labelled `{handler,method,status}` — the
  instrumentator's defaults, kept as-is (ADR-040). The two histograms are missing and are added here.
  **[correction 3]** The gauge is declared but nothing sets it; it is fed from
  `ResultAssembler._budgets`, which is the one place already calling `TokenBucketRateLimiter.remaining()`
  per connector per query (ADR-043).

## Load test (`load/query_load.js`, k6)
- Drive ~500 **RPS** against `POST /v1/query` for 60s (local acceptable), sending the canonical query with a
  high `max_staleness_ms` so cache hits dominate (the run should not be measuring the mock's simulated latency).
  **[correction 6]** `constant-arrival-rate`, not a VU ramp: VUs are a concurrency pool, RPS is an arrival
  rate, and the brief (line 160) asks for the rate. Report `dropped_iterations` alongside p(95) — k6 drops
  iterations it cannot start, so an under-delivered run can otherwise post a flattering percentile (ADR-044).
  **[correction 2]** The "simulated latency" this parenthesis refers to does not exist yet; it is added in
  this phase, on the live path only, so cache hits stay ~0.4ms and this sentence becomes true (ADR-038).
- **Use the `tenant_load` persona, not `alice`/`tenant_acme`.** `tenant_acme`'s GitHub budget is deliberately
  5 req/60s for the 429 demo; pointing 30 000 requests at it would return 429 for essentially the whole run and
  measure nothing. `tenant_load` is seeded with a large budget for exactly this (Phase 1).
- **Run k6 from its container** (`grafana/k6` via compose or `docker run -i`), so `make load` works on a fresh
  clone without the reviewer installing k6 — the sub-60s quickstart claim depends on it.
- **Batch the audit write — but only once measured.** `AuditLogger` does one synchronous Postgres `INSERT`
  per query; the claim is that at 500 QPS that insert *is* the P95. Push it to a bounded `asyncio.Queue`
  drained by a background task (drop-oldest with a counter when full — LAW 4: surface the drop, never
  silently swallow it).
  **[correction 5]** Deferred behind the measurement rather than done first (ADR-042). This is the phase
  whose deliverable is evidence, the prediction is checkable in one k6 run, and LAW 5 forbids building
  against a hypothesis that cheap to test. If the run shows it dominates, this fix ships exactly as written
  above, with a before/after number in the README.
- Thresholds: `http_req_duration p(95) < 1500ms`; a separate scenario that intentionally drains the bucket
  asserts the response is a clean `429` (checked, not counted as a failure).
  **[correction 6, cont.]** The threshold is **not** tuned to pass. If a single uvicorn worker cannot hold
  500 RPS under 1500ms, that is the finding — it goes in `docs/k6-summary.txt` and the README verbatim, with
  the waterfall explaining where the time went.
- Output the k6 summary to `docs/k6-summary.txt`.

## Fresh-clone prerequisites (verify, don't assume)
The quickstart claim is *cold clone → serving in < 60s*, so anything a reviewer must install by hand is a bug:
- `make e2e` must run `playwright install --with-deps chromium` (idempotent) before the specs, or the first run
  fails on a machine that has never run Playwright.
  **[correction 7]** Phase 3's, not this phase's — `make e2e` is still a stub and `tests/e2e/` does not
  exist, so Phase 4 cannot verify it. Carried forward to Phase 3's gate; Phase 4 verifies the two below.
- `make load` must not require a local k6 binary (see above).
- `make up` must not require a local Python — everything runs in the `app` container.

## README.md (the 60-second story)
1. **Quickstart:** `make up && make seed` → open the console → Run. Under 60s cold.
2. **What it proves:** the five hard parts, each with the one command/click that shows it (cross-ref HLD §1).
3. **Trade-offs:** the design doc's five invariants in two sentences each; why federate-live is the default and when we'd materialize.
4. **Prod-mapping of every non-goal (HLD §7):** mock→live connector (the `fetch` seam), Fernet→Vault+KMS, mock-JWT→OIDC, 429-fail-fast→async `202+job_id`, in-memory→spill, docker-compose→k8s/Helm/Terraform. One line each.
5. **Access:** grant repo read to `souvik-sen@ema.co` and `careers@ema.co`.

## Acceptance (gate)
- `make load` produces a summary with P50/P95 and a clean 429 under the drain scenario.
- `/metrics` scrape shows the connector histograms + the `rate_limit_remaining` gauge; a smoke test asserts
  they exist and that a query's `trace_id` resolves to a span set.
  **[correction 3, cont.]** "asserts they exist" is the wording that let the current test pass on a `# HELP`
  line. The gate is a **labelled sample with a numeric value**, recorded by a query in the same test — a
  metric name in a scrape proves only that someone declared it.
- `docs/` contains the trace waterfall and `k6-summary.txt` (both required — lines 52/167), plus
  `docs/demo-output.txt` from Phase 2. The Phase-3 console screenshot is included **if** Phase 3 has run — it is a
  bonus, not a gate, since the brief asks for a *metrics or trace* screenshot.
- Fresh clone → `make up` → serving in < 60s (the reviewer test).

## Done when
The submission repo is self-describing: a reviewer clones, `up`s in under a minute, clicks the console, reads the trace to see "P95 was Jira not the engine," and finds every production concern the prototype didn't build mapped to where the full design covers it.
