# Prototype Execution Plan

> **This document:** the build order. Five phases, each with **what we build · what to expect · how to verify**.
> Phases are dependency-ordered so each one is demoable before the next starts. The per-phase *agent-buildable*
> specs live in `phases/phase-{0..4}-*.md`. Reconciliation with your `01`–`05` idea docs is §B; the finalized
> stack + decisions are §A. Read [`00-PROTOTYPE-HLD.md`](./00-PROTOTYPE-HLD.md) first.

---

## Phase order & dependency spine

```
Phase 0 ──► Phase 1 ──► Phase 2 ──► Phase 3 ──► Phase 4
Scaffold    Connectors  SQL         UI +        Observability
+ contracts + governance pipeline    Playwright  + load + artifacts
   │            │           │           │            │
 up/health   429 + cache  entitled    masked col   trace waterfall
 401/200     unit-green   join green  in browser   + k6 + README
```

Each phase ends at a **green gate** (its verify step). A coding agent should not start phase N+1 until phase N's gate passes. Hour estimates mirror your `05-plan.md`; treat them as ordering, not deadlines.

Two things sit outside this spine and are easy to lose:
- **A spike before Phase 0.** The riskiest work (sqlglot qualify → RLS injection → predicate split → DuckDB) is
  in Phase 2, behind ~4h of plumbing. `phases/phase-0-scaffold.md` now opens with a 60–90 min throwaway spike
  that proves it on hardcoded dicts. If it fails, the plan changes — learn that at hour 0, not hour 5.
- **The submission gate.** Phase gates say "this phase works"; they never say "we can submit."
  [`02-DEFINITION-OF-DONE.md`](./02-DEFINITION-OF-DONE.md) is that gate, plus the MUST/SHOULD/COULD tiers and
  the cut order that protect the submission when the hour boxes slip. Read it before Phase 0, not after Phase 4.
- **The build loop.** [`03-BUILD-PROCESS.md`](./03-BUILD-PROCESS.md) is the command sequence per phase
  (lightweight research → architecture formalization → spec → plan → build), what each step may and may not
  decide, and the phase tracker.

---

## Scope ledger — what each phase buys, and what's left

Read this to answer *"where am I and what's remaining?"* without re-reading five specs. Tiers are from
[`02-DEFINITION-OF-DONE.md`](./02-DEFINITION-OF-DONE.md) §3.

| Phase | Delivers | Tier | Gate (how you know it's done) | Blocks |
|---|---|---|---|---|
| **0** | AST spike · compose+Makefile · contract models · JWT auth · control-plane read layer · migrations · README skeleton | **MUST** | spike prints a correct predicate split; `make up` healthy; 401 vs 200 shell | everything |
| **1** | 2 mock adapters · capability models · token bucket (+burst) · freshness cache · Fernet secrets · YAML seed | **MUST** | 7 unit tests green; cross-tenant cache isolation holds | P2 |
| **2** | parse → entitle → plan → execute · DuckDB join · envelope assembly · audit · **`make demo`** | **MUST** | canonical query entitled end-to-end; alice 3 / bob 1; timeout → partial; trichotomy distinct | P3, P4 |
| **3** | query console · Playwright specs · console screenshot | **SHOULD** | 6 specs green headless | nothing |
| **4** | OTel spans · `/metrics` · k6 load · README completion · artifacts | **MUST** (k6, metric, trace, README) | trace waterfall readable; k6 summary; fresh clone `make up` < 60s | submission |

**Phases 3 and 4 are independent of each other** — both need only Phase 2. That matters, because as numbered the
plan does the SHOULD-tier phase *before* the phase holding four MUST-tier deliverables. If time gets tight,
**do Phase 4 before Phase 3.** The console is the nicest way to show the work; the trace screenshot, the k6 run,
the Prometheus metric and the README are the ones the brief actually requires (lines 160–161, 166–167).
`make demo` from Phase 2 already covers the "show me it working" need without any UI.

### If you only have N hours

| Budget | Do | Result |
|---|---|---|
| ~4h | P0 + P1 | plumbing proven, nothing demoable — **not submittable** |
| ~7h | P0 + P1 + P2 | `make demo` proves all five hard parts via curl; tests green. **Minimally submittable** |
| ~9h | + P4 | every MUST met: k6, metric, trace, README, screenshot. **Properly submittable** |
| ~12h | + P3 | console + Playwright. The version that demos well |
| more | COULD list | ETag/304, nested buckets, circuit breaker, crypto-shred (DoD §3) |

Cut from the bottom. Never cut a MUST to reach a SHOULD.

---

## Phase 0 — Scaffold & contracts  *(≈ hour 1–2)*
**Spec:** [`phases/phase-0-scaffold.md`](./phases/phase-0-scaffold.md)

**What we build**
- `docker-compose.yml`: `app` (FastAPI), `postgres`, `redis`. `Makefile`: `up / seed / test / e2e / load`.
- FastAPI app factory; `POST /v1/query` and `POST /v1/auth/mock-token` routes (query returns a 200 *shell* for now).
- The **contract layer up front**: Pydantic models for the request, the full response envelope (§4 of HLD), and the six-code error vocabulary. Everything downstream fills this contract.
- `AuthContextExtractor`: decode the HS256 mock JWT → `UserContext {tenant_id, user_id, roles, ...}`; 401 on bad/expired.
- Postgres seed scaffolding (empty tables + migration runner; data seeded in Phase 1/2).

**What to expect**
- `make up` → all three containers healthy; `GET /healthz` 200.
- `POST /v1/auth/mock-token {user:"alice", role:"support", tenant:"tenant_acme"}` → a signed JWT.
- `POST /v1/query` with no/invalid token → **401** with a typed error body; with a valid token → **200** shell `{rows:[], ...envelope skeleton, trace_id}`.

**How to verify** — unit test on `AuthContextExtractor` (valid/expired/wrong-aud); one integration test hitting the two routes; `make up` cold-start smoke.

---

## Phase 1 — Connectors + governance  *(≈ hour 2–4)*
**Spec:** [`phases/phase-1-connectors.md`](./phases/phase-1-connectors.md)

**What we build**
- `BaseConnectorAdapter` contract: `fetch(predicates, projection, page) → AdapterResponse`, `capabilities() → CapabilityModel`, `health()`.
- `GitHubConnectorAdapter` + `JiraConnectorAdapter` (mock): deterministic seed datasets (§6 HLD), simulated **pagination** (cursor/`has_more`), per-column **capability model** (`KeyColumns{Require, Operators}`), **error mapping** to the shared vocabulary, and a configurable simulated latency + forced-timeout hook (for the Phase 2 partial-result test).
- `TokenBucketRateLimiter` (Redis, atomic Lua): three nested buckets (tenant → connector → user), sized from `rate_limit_policies`.
- `FreshnessCacheManager` (Redis): key `hash(connector, normalized-request, tenant_id, entitlement_scope)`; stores `{fetched_at, etag, data}` with TTL; conditional-request (ETag → 304) path.
- `SecretsManagerClient`: resolve a tenant's `secret_ref` → Fernet-decrypt → mock token. Seed the Fernet-encrypted secrets.

**What to expect**
- `github_adapter.fetch(page=…, limit=5)` returns exactly 5 rows + a `next_cursor`/`has_more`.
- Draining a bucket → the adapter raises `RateLimitExhausted(retry_after_ms=…)`.
- Two fetches inside TTL → second is a cache hit (no "live" call recorded); after TTL, a conditional request; a `304` refreshes `fetched_at` without spending a token.
- Cache key includes `tenant_id` + `entitlement_scope` → `tenant_globex` never reads `tenant_acme`'s cached rows.

**How to verify** — unit tests: pagination exactness; bucket depletion → 429 signal; cache hit within TTL + 304 revalidation; **cross-tenant cache isolation** (the entitlement-trap test); Fernet round-trip resolves the right tenant's secret.

---

## Phase 2 — SQL pipeline: parse → entitlement → plan → execute  *(≈ hour 4–6)*
**Spec:** [`phases/phase-2-sql-pipeline.md`](./phases/phase-2-sql-pipeline.md)

**What we build**
- `SQLParser` (sqlglot): parse → validate against the supported subset (SELECT/WHERE/JOIN/ORDER BY/LIMIT; no writes/DDL/arbitrary functions) → extract referenced tables + predicates + projection.
- `EntitlementEngine`: fetch applicable policies (JSONB AST) from Postgres for the query's connectors/resources scoped to `UserContext.roles`; compile **RLS → a Filter predicate AND-ed into the plan** and **CLS → a Project mask**; apply **deny-overrides + default-deny**; mask a join-key column only in the *final* projection.
- `QueryPlanner`: capability-check each predicate; split pushable predicates per source; enforce the **projection-union guard** (fetch = projection ∪ every WHERE/ORDER BY column); keep join + residual LIMIT for the engine; the `LIMIT`-can't-push-through-join + `ORDER BY`-needs-final-sort caveats.
- `FederationEngine` (DuckDB): load per-source rows into transient tables, run join/sort/limit/project, assemble the envelope; compute `freshness_ms = now − min(fetched_at)`; set `join_status`/`partial`.
- `QueryPipelineRunner` wires stages 2–5; `AuditLogger` writes one row per query.

**What to expect**
- Canonical query as **alice** → the joined, entitled rows; `reporter_email` masked; `freshness_ms` = the stalest side.
- Same query as **bob** (assigned nothing in scope) → fewer/zero rows — *the RLS is visible in the row count*, and the forbidden rows were **never fetched** (assert the Jira mock received `assignee=currentUser()`).
- Jira forced to time out → `partial: true`, `join_status: incomplete`, `reason: SOURCE_TIMEOUT`, GitHub rows present — never the un-joined set passed off as joined.
- Malformed/unsupported SQL → top-level error, no rows (distinct from empty).

**How to verify** — unit: RLS predicate AST injection; CLS mask in projection; projection-union guard adds the missing WHERE/ORDER BY columns; deny-overrides. Integration: the canonical query end-to-end for alice vs bob; the timeout→partial case; the empty vs partial vs error trichotomy.

---

## Phase 3 — UI console + Playwright E2E  *(≈ hour 6–8)*
**Spec:** [`phases/phase-3-ui-e2e.md`](./phases/phase-3-ui-e2e.md)

**What we build**
- A minimal single-page **query console** (`ui/`): SQL editor (pre-filled with the canonical query), a persona/token control (mint alice vs bob vs a `tenant_globex` user via `/v1/auth/mock-token`), a `max_staleness_ms` field, a **Run** button; a results **table** + a **metadata panel** rendering `freshness_ms`, `rate_limit_status`, `sources[].served`, `join_status`, `partial`, `warnings`, `trace_id`.
- Error/warning surfacing: a 429 renders a rate-limit banner with the `Retry-After`; a `STALE_DATA` warning renders a staleness chip; a masked column renders as `••••`/`null`.

**What to expect** (in the browser)
- Run canonical as alice → rows appear; `reporter_email` visibly masked; metadata panel populated.
- Switch to bob → row set visibly shrinks (RLS).
- Set `max_staleness_ms` high → panel shows `served: cache`; set it to 0 → `served: live` and `connector_ms` populated (freshness knob is visible).
- Spam Run to drain the bucket → a **429 banner** with a retry hint appears instead of a spinner that hangs.

**How to verify** — **Playwright** specs, one per behavior above (rows-render, RLS-shrinks-rows, mask-visible, freshness-knob-flips-served, rate-limit-banner). Playwright runs headless in CI against `make up`. A GIF/screenshot of the console is a submission artifact.

---

## Phase 4 — Observability, load & artifacts  *(≈ hour 8–10)*
**Spec:** [`phases/phase-4-observability.md`](./phases/phase-4-observability.md)

**What we build**
- OpenTelemetry spans around each stage: `gateway_ms`, `parse_ms`, `entitlement_ms`, `connector_github_ms`, `connector_jira_ms`, `duckdb_join_ms`; `trace_id` echoed in the envelope. A `/metrics` Prometheus endpoint (golden signals + a per-connector `rate_limit_remaining` gauge).
- A **k6** load script (`load/`): ~500 QPS for 60s against `POST /v1/query` (local acceptable), reporting P50/P95 + error rate.
- `README.md`: 60-second `docker-compose up` quickstart, the trade-offs rationale, and the **prod-mapping of every non-goal** (§7 HLD).

**What to expect**
- A trace waterfall that makes *"the P95 was Jira, not the engine"* readable at a glance.
- k6 summary: P95 within target on cache-hit-dominated load; clean `429`s (not errors) when a bucket drains.
- `docker-compose up` cold → serving in < 60s.

**How to verify** — capture the trace-waterfall screenshot + the k6 summary + a `/metrics` scrape as artifacts (they land in the README). A smoke test asserts `/metrics` exposes the connector spans and the query envelope carries a resolvable `trace_id`.

---

## §A — Finalized stack & decisions

| Concern | Choice | Source |
|---|---|---|
| Language / API | Python 3.11 + FastAPI (async, Pydantic) | your `03` |
| SQL parse/AST | sqlglot | your `03` |
| Join engine | DuckDB in-memory | your `01`/`03` |
| Rate-limit + cache state | Redis (docker-compose) | your `02`/`03` |
| Control plane | Postgres, seeded at boot | your `02` |
| Secrets | **Fernet-encrypted in Postgres** (Vault mapped in README) | your `03` Option B |
| Connectors | **mock-only**, deterministic | your `01` + this session |
| Policy storage | **JSONB predicate AST** | design-doc §3.2 (diverges from `02`) |
| Join predicate | **equijoin `pr.issue_key = issue.key`** | design-doc §6.1 (diverges from `01`) |
| RLS / CLS | **`assignee=:user` / mask `reporter_email`** | design-doc §3.2/§6.1 (diverges from `01`) |
| UI test framework | **Playwright** | this session |
| Observability | OpenTelemetry + Prometheus `/metrics` | your `01`/`05` |
| Load | k6, ~500 QPS/60s | your `05` |
| Class taxonomy | `QueryPipelineRunner` + adapters + governance | your `04` (adopted) |

## §B — Reconciliation with your `01`–`05` idea docs

**Adopted as-is:** the FastAPI/sqlglot/DuckDB/Redis/Postgres stack (`03`); docker-compose + the Postgres/Redis schemas and Redis key designs (`02`); deterministic mock connectors + token bucket + folder shape (`01`); the class taxonomy + DTOs (`04`); the hour-boxed 5-phase structure (`05`).

**Deviated — with reason:**

| # | Idea doc | What it proposed | Prototype does | Why |
|---|---|---|---|---|
| 1 | `01` | Join `gh.title LIKE '%'||jira.issue_key||'%'` | Equijoin `pr.issue_key = issue.key` (mock PRs carry a derived `issue_key`) | Matches design-doc §6.1 worked example + envelope; both deliverables tell one story; a fuzzy LIKE join is harder to reason about and to test deterministically. |
| 2 | `02` | Policy as `rls_filter_sql TEXT` (raw SQL) | Policy as **JSONB predicate AST** | Design-doc §3.2 chose AST explicitly: zero SQL-injection surface, and the planner can safely rewrite/push it. A raw string can't be pushed down safely. |
| 3 | `01` | RLS `repo IN (allowed_repos)`, mask `assignee` | RLS `jira.issues.assignee=:user`, mask `reporter_email` | Consistency with the design doc's canonical RLS/CLS and §6 envelope. `reporter_email` lives on Jira issues; PRs have no reporter. |
| 4 | `03` | Vault container recommended (Option A) | **Fernet-encrypted secret in Postgres** (Option B) | 6–10h budget; Option B is your docs' own fallback. README maps `secret_ref` → Vault KV + KMS for prod. |
| 5 | `05` | UI mentioned, no E2E framework | UI console + **Playwright** specs as a first-class gate | This session's explicit ask; gives a visible, testable demo of RLS/CLS/freshness/rate-limit. |
| 6 | `02` | `connectors` table has `tenant_id` FK + per-tenant `base_url` | Split: `connectors` = **global** catalog (type, version, capabilities); `tenant_connector` = per-tenant **grant + `secret_ref`** | Matches design-doc §8.3 control-plane DDL; keeps the connector *definition* (data-not-code) separate from a tenant's *grant* of it. |
| 7 | `01` | join key note: LIKE-based, `LIMIT 10` inside subquery | `LIMIT` applied **after** the join; `ORDER BY`+`LIMIT` re-sorted in-engine | Design-doc §4.4 correctness caveats: pushing LIMIT through a join drops rows that would have joined. |

**Deferred to the "later" pile (your call, not built now):** live connectors (no Jira sub yet), async `202 + job_id` reroute, on-disk materialization spill, real OIDC/Vault/KMS, k8s/Helm/Terraform. Each gets a README paragraph mapping it to the full design.

## §D — OSS research hardening (see `research/prototype-prior-art.md`)

Four repos deep-read (`gh` + `repomix`/shallow-clone) to validate build choices; all High confidence (sqlglot APIs runtime-executed). What changed:
- **sqlglot** (Card 1) → Phase 2: mandatory `qualify()` first; **manual** per-source predicate split (NOT sqlglot's `pushdown_*` optimizer passes — wrong abstraction); `(db,name)` table mapping; AST-node-whitelist subset validator; RLS via `exp.EQ`+`.where()`, CLS via `proj.replace(alias_(func(...)))`.
- **airbyte-python-cdk** (Card 2) → Phase 1: one `RequestOption` injection primitive folded into the capability model; pagination = strategy ⊕ placement + explicit stop; error mapping = action-enum + `failure_type` + default status→action table; `Retry-After` header-driven backoff.
- **universql** (Card 3) → Phase 2: `duckdb.register(name, pyarrow.Table)` per source → run SQL over registered names; `:memory:` per request; validated the DuckDB-federation shape.
- **fastapi-permissions** (Card 4) → Phase 2 note: adopt only its `configure_permissions` DI factory; it's object-level/post-fetch, so we deliberately do NOT use it — confirms the compile-into-plan stance.

## §C — Take-home requirements → coverage (traceability)

Audited against `./take_home.md`. The detailed **Prototype Requirements (lines 150–161)**, **Minimal expectations (61)**, and **Submission Checklist (163–167)** are the prototype bar; everything below is covered by a phase gate.

| Take-home requirement (line) | Phase / where |
|---|---|
| `POST /v1/query` → rows + columns, freshness_ms, rate_limit_status, trace_id (152) | P0 envelope model; P2 assembly |
| sql **or** plan JSON (152) | P0 `QueryRequest` accepts `sql` (the "or" makes plan-JSON optional; documented choice) |
| 1 RLS rule + 1 column mask config (154) | P1 `002_seed.sql` (JSONB AST) |
| 2 connectors, real or mocked (156) | P1 GitHub + Jira mocks |
| user token → scopes/roles → RLS/CLS (157) | P0 JWT→UserContext; P2 EntitlementEngine |
| token bucket **with burst**; friendly error + **async reroute guidance** (158) | P1 `TokenBucketRateLimiter` (burst in `rate_limit_policies`); P0 429 `suggested_action` names async |
| max_staleness knob; **cache hit vs live** (159) | P1 FreshnessCache; P0 request; P3 `freshness_knob` Playwright |
| k6 ~500–1k QPS / 60s (160) | P4 `load/query_load.js` |
| 1 Prometheus metric + 1 trace showing connector time (161) | P4 `/metrics` + OTel `connector_*_ms` |
| SQL: projection, filters, **pagination**, joins (19) | P2 subset + `next_cursor` result pagination; P1 connector pagination |
| real-time: timeouts + partial results (24) | P0 request deadline; P2 timeout→partial (`test_timeout_partial`) |
| error vocabulary (67) | P0 six-code enum |
| join strategy documented (69) | HLD §2; design-doc §4.4 |
| quickstart (containerized) + 1–2 tests (51,166) | P0 docker-compose/Makefile; tests across P0–P4 |
| screenshot of metrics/trace + note (52,167) | P4 `docs/` + README |
| grant read to souvik-sen@ / careers@ (50,165) | P4 README; design-doc §6.4 |
| admins onboard connectors via **config**; connectors **versioned** (29–30) | P1 `config/connectors/*.yaml` → `connectors.capabilities` JSONB + `.version`; admin *console* is a stated non-goal (HLD §7) |
| **cost controls** in the HLD (10, 38, bonus 177) | design-doc scope, not prototype: Risk 6 + §5.2 + cost table. Gap noted in `02-DEFINITION-OF-DONE.md` §6 — worth one dedicated subsection in the submitted doc |

**Stated non-goals (built as prose in the README, not code)** — deployment modes without code changes (31), async job runner (92), materialization spill (95), Vault+KMS (99), IaC/Helm/canary (125–126), DR multi-region (127): these are **HLD/design-doc** scope; the prototype maps each to its production counterpart per HLD §7.
