# Phase 4 — Observability, Load & Artifacts

> **Goal:** make performance legible and package the submission. **Gate:** a trace waterfall that shows where time
> went, a k6 run at ~500 QPS, and a README that lets a reviewer `docker-compose up` in < 60s. Depends on Phase 2/3.

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
- Ramp to ~500 VUs/RPS against `POST /v1/query` for 60s (local acceptable), sending the canonical query with a valid alice token and a high `max_staleness_ms` (so cache-hit dominates and the run isn't just measuring the mock's forced latency).
- Thresholds: `http_req_duration p(95) < 1500ms`; a separate scenario that intentionally drains the bucket asserts the response is a clean `429` (checked, not counted as a failure).
- Output the k6 summary to `docs/k6-summary.txt`.

## README.md (the 60-second story)
1. **Quickstart:** `make up && make seed` → open the console → Run. Under 60s cold.
2. **What it proves:** the five hard parts, each with the one command/click that shows it (cross-ref HLD §1).
3. **Trade-offs:** the design doc's five invariants in two sentences each; why federate-live is the default and when we'd materialize.
4. **Prod-mapping of every non-goal (HLD §7):** mock→live connector (the `fetch` seam), Fernet→Vault+KMS, mock-JWT→OIDC, 429-fail-fast→async `202+job_id`, in-memory→spill, docker-compose→k8s/Helm/Terraform. One line each.
5. **Access:** grant repo read to `souvik-sen@ema.co` and `careers@ema.co`.

## Acceptance (gate)
- `make load` produces a summary with P50/P95 and a clean 429 under the drain scenario.
- `/metrics` scrape shows the connector histograms + the `rate_limit_remaining` gauge; a smoke test asserts they exist and that a query's `trace_id` resolves to a span set.
- `docs/` contains the trace waterfall, `k6-summary.txt`, and the Phase-3 console screenshot.
- Fresh clone → `make up` → serving in < 60s (the reviewer test).

## Done when
The submission repo is self-describing: a reviewer clones, `up`s in under a minute, clicks the console, reads the trace to see "P95 was Jira not the engine," and finds every production concern the prototype didn't build mapped to where the full design covers it.
