# Prototype HLD — Universal SQL (GitHub PRs ↔ Jira Issues)

> **This document:** the high-level design for the *runnable prototype* (design-doc deliverable #3). It is a
> deliberately scoped-down slice of [`./design-doc.md`](./design-doc.md) — enough to *prove* the five hard
> parts on one real query, not to ship the platform. Read this first, then [`01-EXECUTION-PLAN.md`](./01-EXECUTION-PLAN.md)
> for build order, then `phases/phase-*.md` for the agent-buildable specs.
>
> **Stack (locked):** Python 3.11 · FastAPI · sqlglot · DuckDB · Redis · Postgres · Fernet · Playwright · k6 · OpenTelemetry + Prometheus. All via `docker-compose`.
> **Scenario (locked):** one canonical cross-app query — open GitHub PRs joined to their in-progress Jira issues, entitled to the caller.
> **Connectors (locked):** **mock-only** (deterministic in-memory datasets). Live GitHub/Jira is an explicit non-goal (see §7).

---

## 1. What the prototype must prove

The full design doc names **five hard parts** that separate a real multi-tenant service from a single-tenant CLI (StackQL/Steampipe). The prototype's job is to make all five *visible and testable* on one query, and nothing more:

| # | Hard part (design doc §1.1) | How the prototype proves it | Rubric axis |
|---|---|---|---|
| 1 | **Query-time RLS/CLS entitlement** | An RLS predicate + a CLS mask compile *into* the plan before fetch; switching the caller visibly changes rows and masks a column | Security 15% |
| 2 | **Per-tenant fairness over rate-limited APIs** | A token bucket returns `429 RATE_LIMIT_EXHAUSTED` + `Retry-After` when a connector budget is drained — never a hang | Rate limits 15% |
| 3 | **Credential isolation** | Per-tenant secret resolved by indirection (Fernet-encrypted in Postgres), never inline | Security 15% |
| 4 | **Entitlement-aware caching / freshness** | A `max_staleness_ms` knob visibly switches cache-hit vs live fetch; `freshness_ms` reports the *stalest* contributor | Freshness 15% |
| 5 | **Connector reliability + honest degradation** | A timed-out source yields `partial: true` / `join_status: incomplete`, not a wrong answer | Architecture 30% / Prototype 15% |

Everything below is in service of these five. If a feature doesn't advance one of them, it is out of scope for the prototype.

## 2. What we keep vs. simplify from the full design

The prototype is the full design's data plane, collapsed into **one process** with the control plane reduced to seed data. The mapping is explicit so a reviewer can see the line from design → prototype and back.

| Full design (design-doc.md) | Prototype | Rationale for the cut |
|---|---|---|
| Control plane: registry, catalog, policy, secrets, rate-limit, audit (Postgres + Vault + KMS) | **Postgres** with seeded tables (`tenants`, `connectors`, `policies`, `rate_limit_policies`, `audit_logs`); secrets **Fernet-encrypted in Postgres** | One store, seeded at boot. Fernet stands in for Vault+KMS — mapped in the README. |
| Data plane: gateway → planner → entitlement → connector workers → assemble, autoscaled, stateless, mTLS | **One FastAPI process**, same five stages as in-process components | The *stages* are the design; horizontal scale + mTLS are asserted, not built. |
| Real OIDC (per-tenant IdP, JWKS) | **Mock JWT** (HS256, minted by `/v1/auth/mock-token`) | Proves identity → RLS/CLS flow without standing up an IdP. Claims shape matches OIDC. |
| Live SaaS connectors under real rate limits | **Mock connectors** with deterministic datasets + simulated pagination/latency/429 | Determinism for tests; no external flakiness; no Jira subscription needed. |
| Per-tenant KMS key → crypto-shred | **Per-tenant Fernet key**; crypto-shred *described*, key-destroy *demonstrable in a test* | The mechanism (per-tenant key ⇒ key-destroy) is shown at prototype scale. |
| Redis: distributed buckets + response cache + async jobs | **Redis**: token buckets + freshness cache (async path stubbed, see §7) | The two hot-path uses are real; async reroute is out of prototype scope. |
| Short-lived DuckDB/Parquet spill, adaptive | **In-memory DuckDB** join every query | Federate-live is the common case; spill is asserted with one code seam, not exercised. |
| k8s namespaces, Helm/Terraform, HPA, canary | **docker-compose**; scaling/topology *described* in README | Prototype proves behavior, not deployment. |

**The invariants that survive the cut unchanged** (these are correctness, not scale): pushdown-as-optimization + the projection-union guard; entitlement compiled-in and pushed-down (never post-filter); transient/encrypted materialization; fail-fast with typed metadata; `freshness_ms = now − min(fetched_at)`.

## 3. Component map (one process, five stages)

```
                         ┌──────────────────────────────────────────────┐
                         │   Postgres (control plane, seeded at boot)    │
                         │   tenants · connectors · policies ·           │
                         │   rate_limit_policies · audit_logs · secrets  │
                         └───────────────┬──────────────────────────────┘
                                         │ read at request time (cached)
  ┌────────┐   POST /v1/query            ▼
  │  UI     │──(SQL + Bearer JWT +  ┌──────────────────────────────────────────────┐
  │ console │   max_staleness_ms)──►│              FastAPI process                  │
  └────────┘                        │                                              │
                                    │  1 QueryGatewayHandler  (authN, subset check)│
                                    │  2 SQLParser (sqlglot)  → AST                │
                                    │  3 EntitlementEngine    (RLS filter + CLS    │
                                    │       mask compiled INTO the AST)            │
                                    │  4 QueryPlanner         (pushdown +          │
                                    │       projection-union guard, capability chk)│
                                    │  5 FederationEngine (DuckDB join/sort/limit) │
                                    └───────┬───────────────────────┬──────────────┘
                                            │ per-source fetch       │
                              ┌─────────────▼──────┐      ┌──────────▼─────────────┐
                              │ GitHubConnector    │      │ JiraConnector          │
                              │ (mock)             │      │ (mock)                 │
                              │  · capability model│      │  · capability model    │
                              │  · pagination      │      │  · pagination          │
                              │  · error mapping   │      │  · error mapping       │
                              └───┬──────────┬─────┘      └────┬──────────┬─────────┘
                                  │          │                 │          │
                          TokenBucket   FreshnessCache   TokenBucket   FreshnessCache
                            (Redis)       (Redis)          (Redis)       (Redis)
```

Class names follow your `04-high_level_class_structure.md` taxonomy (`QueryPipelineRunner` orchestrates stages 2–5; `BaseConnectorAdapter` is the connector contract; `TokenBucketRateLimiter`, `FreshnessCacheManager`, `SecretsManagerClient`, `AuditLogger` are cross-cutting). The one addition is the **UI console** (React/HTML) exercised by Playwright.

## 4. The canonical query & data contract

**The one query the prototype runs end-to-end** (identical to design-doc §6.1 — the two deliverables must match):

```sql
SELECT pr.title, pr.author, issue.key, issue.status
FROM   github.pull_requests pr
JOIN   jira.issues issue ON pr.issue_key = issue.key
WHERE  pr.repo = 'ema/core' AND pr.state = 'open' AND issue.status = 'In Progress'
ORDER BY issue.updated DESC
LIMIT 50;
```

- **Join** is a clean **equijoin** `pr.issue_key = issue.key` (not a fuzzy `title LIKE`). Mock PR rows carry a derived `issue_key` field; in production this is extracted from the PR branch/title (noted as a realism caveat, not built).
- **RLS** (canonical): `jira.issues.assignee = :user` — pushed into JQL as `assignee = currentUser()`.
- **CLS** (canonical): mask `jira.issues.reporter_email` (mask kinds: `null | hash | redact | drop`).
- **Pushdown split**: GitHub gets `repo='ema/core' AND state='open'`; Jira gets `status='In Progress' AND assignee=currentUser() ORDER BY updated DESC`; the engine keeps only the join on `issue_key` + residual `LIMIT 50`.
- **Pagination** (functional requirement): the SQL subset supports `LIMIT`; the caller pages the *joined result* with `next_cursor` (an opaque offset over the sorted, entitled rows). Connector-level pagination (Link-header / `startAt`) is separate and lives in the adapters (§3). Both are exercised.

**Response envelope** (the shape every request returns — matches design-doc §6.2; this is a first-class deliverable, typed as Pydantic models):

```jsonc
// POST /v1/query   { "sql": "...", "max_staleness_ms": 60000 }
// 200
{
  "columns": [ {"name":"title","type":"string","source":"github"},
               {"name":"status","type":"string","source":"jira","masked": false} ],
  "rows": [ ["Fix retry","alice","SUP-12","In Progress"] ],
  "freshness_ms": 8200,                          // now − min(fetched_at) across contributors
  "rate_limit_status": { "github": {"remaining":4870,"throttled":false},
                         "jira":   {"remaining":1180,"throttled":false} },
  "sources": [ {"connector":"github","state":"ok","served":"live"},
               {"connector":"jira","state":"ok","served":"cache"} ],
  "join_status": "complete",                     // complete | incomplete
  "partial": false,
  "next_cursor": "eyJvZmZzZXQiOjUwfQ==",         // result-level pagination over the joined rows (null when exhausted)
  "warnings": [],
  "trace_id": "abc123",
  "stats": { "connector_ms": {"github":120} }    // jira from cache → no live call
}
```

**Error vocabulary** (exactly the design doc's — one vocabulary, not three): `RATE_LIMIT_EXHAUSTED` (429, +Retry-After), `STALE_DATA` (200 warning), `ENTITLEMENT_DENIED` (403), `SOURCE_TIMEOUT` (200 partial / 504), `CONNECTOR_NOT_ENABLED` (403, gateway), `CONNECTOR_AUTH_ERROR` (403, gateway).

## 5. The three outcomes, kept distinct

The prototype must keep **empty ≠ partial ≠ error** distinct (design-doc §4.4) — this is a graded correctness point, not a nicety:
- **Empty** — query ran, filters/entitlement left zero rows → `rows: []`, `partial: false`. A *correct* answer.
- **Partial** — a source timed out/throttled → `partial: true`, and `join_status: incomplete` if a *joined* side is missing. A *degraded* answer.
- **Error** — auth failure on a required connector, malformed SQL → top-level error code, no rows.

For a join specifically: if Jira times out we **never** pass the un-joined GitHub rows off as the joined answer — we return the driving side with `join_status: incomplete` + `reason: SOURCE_TIMEOUT`, or empty+partial. The prototype has a test for exactly this.

## 6. Seed data (deterministic, checked into the repo)

- **Tenants:** `tenant_acme` (multi-tenant default). One tenant is enough to prove per-tenant keying; a second (`tenant_globex`) is seeded *only* to prove cache/credential isolation in a test.
- **Users (JWT personas):** `alice` (assigned SUP-12, SUP-13) and `bob` (assigned nothing in `ema/core`) — the RLS demo is "alice sees rows, bob sees fewer/none."
- **GitHub mock:** ~20 PRs in `ema/core`, mix of open/closed, each with `issue_key` linking to a Jira issue (some to in-progress, some not).
- **Jira mock:** ~20 issues, mix of statuses/assignees, each with a `reporter_email` (the CLS target).
- **Policies (seeded):** the 1 RLS + 1 CLS rule above, as JSONB AST (design-doc §8.2 shape).
- **Rate-limit budgets:** small enough that a short test loop drains a bucket and triggers the 429 path deterministically (e.g. GitHub Search-class ~30/min → set to a handful for the test).

## 7. Explicit non-goals (say them, don't build them)

Each non-goal gets one README paragraph pointing at where the full design covers it — this is how the prototype stays honest without looking thin:
- **Live connectors** — mock-only. *Where live plugs in:* `BaseConnectorAdapter` has a single `fetch(predicates, projection, page)` seam; a live adapter implements the same contract behind the same capability model. No Jira subscription needed for the prototype.
- **Async reroute / job queue** — the `202 + job_id` path (design §4.2) is described, not built; the rate-limit path in the prototype fails fast with `429 + Retry-After` (whose `suggested_action` still names async).
- **DRR fair scheduler + per-connector bulkheads** — the design's fairness *policy* (design §4.1) is a multi-worker scheduling concern with no meaning in a single process. The prototype proves fairness at the **token-bucket** layer (per tenant→connector→user); DRR + bulkheads are described in the README, not built.
- **Residency enforcement** — `tenant.residency` is seeded and audited, but placement enforcement (region-pinning storage/compute) is meaningless in a single local deployment; described, not enforced.
- **Materialization spill to disk** — DuckDB joins in-memory; the spill seam is a documented code path, not exercised.
- **Real OIDC / Vault / KMS** — mock JWT + Fernet; the README maps each to its production counterpart.
- **k8s / Helm / Terraform / autoscaling** — described in README + design §5, not built.

## 8. Repo layout (target — the coding agent builds into this)

```
universal-sql-prototype/
├── docker-compose.yml          # app · postgres · redis
├── Makefile                    # make up / seed / test / e2e / load
├── README.md                   # 60-second quickstart + trade-offs + prod-mapping of every non-goal
├── pyproject.toml
├── src/
│   ├── main.py                 # FastAPI app factory + routes
│   ├── gateway/                # QueryGatewayHandler, AuthContextExtractor, mock-token
│   ├── pipeline/               # QueryPipelineRunner (orchestrator)
│   ├── sqlparse/               # SQLParser (sqlglot): parse, validate subset, extract tables/predicates
│   ├── entitlement/            # EntitlementEngine: RLS predicate + CLS mask INTO the AST
│   ├── planner/                # QueryPlanner: pushdown split + projection-union guard + capability check
│   ├── execution/              # FederationEngine (DuckDB)
│   ├── connectors/             # BaseConnectorAdapter, GitHubConnectorAdapter, JiraConnectorAdapter, mock data
│   ├── governance/             # TokenBucketRateLimiter, FreshnessCacheManager, SecretsManagerClient, AuditLogger
│   ├── models/                 # DTOs: UserContext, QueryPlan, AdapterResponse, QueryExecutionResult, envelope
│   ├── control_plane/          # Postgres access + seed scripts
│   └── observability/          # OTel spans + /metrics
├── ui/                         # query console (SQL editor, token field, table + metadata panel)
├── tests/
│   ├── unit/                   # token bucket, cache, RLS/CLS AST injection, planner guard
│   ├── integration/            # full pipeline on the canonical query
│   └── e2e/                    # Playwright specs
└── load/                       # k6 script (~500 QPS / 60s)
```

## 9. Provenance / consistency rails (do not drift)

Every value below is fixed and must be identical across the design doc, this HLD, and all phase specs:
- Canonical query = the 4-column equijoin in §4. `repo` is required on GitHub (path param).
- RLS = `jira.issues.assignee = :user`; CLS = mask `jira.issues.reporter_email`. `reporter_email` lives on **Jira issues only** (PRs have no reporter).
- `effect` enum = `allow | deny` (deny-overrides, default-deny). `mask` enum = `null | hash | redact | drop`.
- Policy predicate is a **JSONB AST**, never a raw SQL string.
- `freshness_ms` = `now − min(fetched_at)` (the *stalest* contributor wins).
- Error vocabulary = the six codes in §4; the first four are query-execution, the last two are gateway.
