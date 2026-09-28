# Phase 4 — Observability, Load & Artifacts

> **Goal:** make performance legible and package the submission. **Gate:** a trace waterfall that shows where time
> went, a k6 run at ~500 QPS, and a README that lets a reviewer `docker-compose up` in < 60s.
> **Depends on Phase 2 only — not on Phase 3.** Four of this phase's deliverables (k6, the Prometheus metric, the
> trace, the README) are MUST-tier while Phase 3 is SHOULD-tier, so **if time is tight, run this phase before
> Phase 3.**

## Deliverables
1. OpenTelemetry spans across the pipeline + `trace_id` propagation.
2. `/metrics` Prometheus endpoint (golden signals + per-connector gauge).
3. `load/` — a k6 script (~500 QPS / 60s).
4. `README.md` — quickstart + trade-offs + prod-mapping of every non-goal.
5. Captured artifacts → `docs/` (trace waterfall, k6 summary, `/metrics` scrape).

## Observability (`src/observability/`)
- Wrap each stage in an OTel span; span names → the envelope's `stats.connector_ms` and a full waterfall: `gateway_ms`, `parse_ms`, `entitlement_ms`, `plan_ms`, `connector_github_ms`, `connector_jira_ms`, `duckdb_join_ms`. The root span's id = the envelope `trace_id`.
- Export to console/OTLP; for the screenshot, a local Jaeger/Tempo container is optional — a structured-log waterfall is acceptable per the take-home.
- `/metrics` (prometheus-client): `http_requests_total{route,code}`, `query_duration_seconds` histogram (→ P50/P95), `connector_fetch_duration_seconds{connector}`, and a `rate_limit_remaining{tenant,connector}` gauge fed from `TokenBucketRateLimiter.remaining()`.

## Load test (`load/query_load.js`, k6)
- Ramp to ~500 VUs/RPS against `POST /v1/query` for 60s (local acceptable), sending the canonical query with a
  high `max_staleness_ms` so cache hits dominate (the run should not be measuring the mock's simulated latency).
- **Use the `tenant_load` persona, not `alice`/`tenant_acme`.** `tenant_acme`'s GitHub budget is deliberately
  5 req/60s for the 429 demo; pointing 30 000 requests at it would return 429 for essentially the whole run and
  measure nothing. `tenant_load` is seeded with a large budget for exactly this (Phase 1).
- **Run k6 from its container** (`grafana/k6` via compose or `docker run -i`), so `make load` works on a fresh
  clone without the reviewer installing k6 — the sub-60s quickstart claim depends on it.
- **Batch the audit write before this runs.** `AuditLogger` as specced does one synchronous Postgres `INSERT`
  per query; at 500 QPS that insert *is* the P95. Push it to a bounded `asyncio.Queue` drained by a background
  task (drop-oldest with a counter when full — LAW 4: surface the drop, never silently swallow it).
- Thresholds: `http_req_duration p(95) < 1500ms`; a separate scenario that intentionally drains the bucket asserts the response is a clean `429` (checked, not counted as a failure).
- Output the k6 summary to `docs/k6-summary.txt`.

## Fresh-clone prerequisites (verify, don't assume)
The quickstart claim is *cold clone → serving in < 60s*, so anything a reviewer must install by hand is a bug:
- `make e2e` must run `playwright install --with-deps chromium` (idempotent) before the specs, or the first run
  fails on a machine that has never run Playwright.
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
- `/metrics` scrape shows the connector histograms + the `rate_limit_remaining` gauge; a smoke test asserts they exist and that a query's `trace_id` resolves to a span set.
- `docs/` contains the trace waterfall and `k6-summary.txt` (both required — lines 52/167), plus
  `docs/demo-output.txt` from Phase 2. The Phase-3 console screenshot is included **if** Phase 3 has run — it is a
  bonus, not a gate, since the brief asks for a *metrics or trace* screenshot.
- Fresh clone → `make up` → serving in < 60s (the reviewer test).

## Done when
The submission repo is self-describing: a reviewer clones, `up`s in under a minute, clicks the console, reads the trace to see "P95 was Jira not the engine," and finds every production concern the prototype didn't build mapped to where the full design covers it.
