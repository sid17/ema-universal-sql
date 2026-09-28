# Phase 0 — Scaffold & Contracts — Design Spec

> **Published:** 2026-09-28 · **Tier:** MUST · **Gate:** `make up` healthy; 401 on bad token, 200 envelope
> shell on good token; all Phase 0 unit + integration tests green.
>
> **Self-contained by intent.** This spec is what `/plan-phase` consumes. Source artifacts are listed at the
> bottom; you should not need to open them to build this phase.
>
> **Conversion, not new design.** `docs/design/phases/phase-0-scaffold.md` is already spec-grade. This adds the
> four things it does not carry: per-task MUST/SHOULD/COULD tiers, explicit file paths, the test list as named
> files, and the README section this phase fills. **Where this spec and the phase file disagree, the phase file
> wins.**

## Context

We are building the runnable prototype for a **federated query layer that runs one cross-app SQL query**
(GitHub PRs ⋈ Jira issues) end-to-end, with query-time entitlement, per-tenant rate limiting,
staleness-controlled caching, and honest partial-result degradation. It is deliverable #3 of a four-part
take-home; this repo is deliverables 3 and 4 (the prototype and its README), while the design doc and the
six-month plan live in a Google Doc.

Phase 0's job is narrow: **an end-to-end skeleton so that every later phase fills a contract that already
exists.** Containers up, auth working, the response envelope typed. No query actually executes this phase —
`/v1/query` returns a 200 *shell*.

Two things make this phase bigger than "scaffolding":

1. **The control-plane read layer (`src/control_plane/repository.py`) lands here.** Three later phases read
   through it, but no phase owned it. Without its in-process TTL cache, every query pays 4–5 Postgres
   round-trips and the Phase 4 P95 measures Postgres rather than Jira.
2. **Observability is front-loaded.** `03-BUILD-PROCESS.md` moves the OTel span decorator and the Prometheus
   registry out of Phase 4 and into Phase 0, so Phase 2 can decorate each stage *as it writes it* rather than
   re-reading five modules later. This is free — no dependency change, no merge conflict — and it shrinks
   Phase 4 to k6 + artifacts + README.

## Task 0 — the de-risking spike ✅ **COMPLETE**

Ran before any infra, as the phase file requires. `spike/ast_spike.py`, no FastAPI/Postgres/Redis/Docker,
hardcoded dicts. **Result: 14/14 checks passed** — the canonical query returns joined rows and the per-source
predicate split is correct (GitHub gets `repo` + `state`; Jira gets `status` + the injected RLS). RLS row
counts landed on HLD §6 exactly: alice 3 / bob 1 / carol 0.

**Phase 2's design does not change.** The spike did find one load-bearing error in research Card 1, already
folded into `phases/phase-2-sql-pipeline.md` and the card:

- `where.this.flatten()` is **not** sufficient. `tree.where(pred, append=True)` routes through `exp.and_`,
  which wraps the existing WHERE in an `exp.Paren`; `flatten()` prunes at the paren and returns the whole
  nested AND as one leaf, yielding **zero** pushable GitHub predicates. It must recurse through `.unnest()`.
  This would not have raised an error in Phase 2 — it would have silently disabled all pushdown while still
  returning correct rows.
- Minor: on duckdb 1.5.x `.arrow()` returns a `RecordBatchReader`; `fetch_arrow_table()` is deprecated. Use
  `to_arrow_table()`.

**Disposition:** promote to `tests/unit/test_ast_spike.py` in this phase (task T12) — it is a genuine
regression test for the pushdown-split behaviour, it runs in milliseconds with no infra, and the commit hook
already runs `tests/unit`. Phase 2 deletes it only if it duplicates a real planner test.

## Architecture

Single FastAPI process. Phase 0 builds the outer shell and the cross-cutting layers; the five pipeline stages
are stubbed and filled in Phases 1–2.

```
src/
├── main.py                      # app factory + route mounting          [T10]
├── models/
│   ├── context.py               # UserContext (frozen dataclass)        [T4]
│   ├── request.py               # QueryRequest (Pydantic)               [T4]
│   ├── envelope.py              # QueryEnvelope + ColumnMeta +          [T4]
│   │                            #   ConnectorBudget + SourceOutcome
│   └── errors.py                # ErrorCode enum + ApiError             [T5]
├── gateway/
│   ├── auth.py                  # mint_mock_token, AuthContextExtractor [T6]
│   ├── deps.py                  # get_current_user FastAPI dependency   [T6]
│   └── handlers.py              # ApiError -> HTTP exception handler    [T5]
├── control_plane/
│   ├── db.py                    # psycopg pool + migration runner       [T7]
│   └── repository.py            # TTL-cached control-plane reads        [T8]
└── observability/
    ├── tracing.py               # tracer provider + @stage_span         [T9]
    └── metrics.py               # registry + rate_limit_remaining gauge [T9]

migrations/001_init.sql          # 7 tables, empty (seeded in P1/P2)     [T7]
docker-compose.yml · Dockerfile  # app + postgres:16 + redis:7           [T2]
Makefile · pyproject.toml        # up/down/seed/test/e2e/load/demo/fmt   [T1,T3]
README.md                        # Quickstart section only this phase    [T11]
spike/ast_spike.py               # done; promoted to tests/unit          [T0]
```

**Request flow this phase:** `POST /v1/query` → JWT decode → `UserContext` → tenant-status gate → attach
per-request deadline + `trace_id` → return the envelope shell. Subset validation, entitlement, planning and
execution are Phase 2.

## Tech stack + key decisions

Sixteen ADRs are Accepted in `docs/kickoff/v1/architecture.md`. The ones that bind *this phase*:

| Decision | Pick | Why |
|---|---|---|
| Runtime | Python 3.11 + FastAPI + Pydantic v2 | sqlglot and DuckDB are Python-first (ADR-001) |
| Control plane | Postgres, seeded, **TTL-cached reads** | one store for 6 concerns; the cache is what makes HLD §3's "read at request time (cached)" true (ADR-005) |
| Secrets | Fernet ciphertext in Postgres, per-tenant key | proves indirection + crypto-shred in ~30 lines; README maps it to Vault+KMS (ADR-006) |
| Catalog model | global `connectors` + `tenant_connector` grant | keeps connector *definition* (data-not-code) separate from a tenant's *grant* (ADR-013) |
| `/metrics` | **one route we own** — `Instrumentator().instrument(app)` for collectors, **not** `.expose(app)`; our route calls `generate_latest(REGISTRY)` | runtime-verified the library shares `prometheus_client`'s default `REGISTRY`, so golden signals and our gauge land in one scrape (ADR-015) |
| Tracing backend | **none in compose** — console/in-memory exporter | compose stays 3 services against the <60s cold-start gate; spans auto-parent and carry durations, so the waterfall data is all present (ADR-016) |
| Trace id | read from the ambient span, `format(ctx.trace_id, "032x")` | makes `QueryEnvelope.trace_id` genuinely resolvable against the exported trace, not a decorative UUID |

**Dependency correction from v1 research:** the phase file's `pyproject.toml` list is **incomplete**.
`FastAPIInstrumentor` ships in its own distribution. Add **`opentelemetry-instrumentation-fastapi`** (pulls
`-api`, `-asgi`, `-semantic-conventions`, `-util-http`) and **`prometheus-fastapi-instrumentator`**.
Instrumentation packages are pre-1.0 and must be upgraded as a set. Verified compatible:
`fastapi 0.141.1` / `starlette 1.7.0` / `opentelemetry-sdk 1.45.0` / instrumentation `0.66b0` /
`prometheus-client 0.26.0` / `prometheus-fastapi-instrumentator 8.1.0`.

## Data contracts

### Response envelope — `src/models/envelope.py`

**This is the contract every later phase fills.** Verbatim from HLD §4 = design-doc §6.2.

```python
class ColumnMeta(BaseModel):
    name: str; type: str; source: str; masked: bool = False
class ConnectorBudget(BaseModel):
    remaining: int; throttled: bool
class SourceOutcome(BaseModel):
    connector: str
    state: Literal["ok", "timeout", "error", "throttled"]
    served: Literal["live", "cache", "none"]
class QueryEnvelope(BaseModel):
    columns: list[ColumnMeta] = []
    rows: list[list] = []
    freshness_ms: int | None = None          # now - min(fetched_at): the STALEST contributor
    rate_limit_status: dict[str, ConnectorBudget] = {}
    sources: list[SourceOutcome] = []
    join_status: Literal["complete", "incomplete", "n/a"] = "n/a"
    partial: bool = False
    next_cursor: str | None = None           # null whenever partial=true
    warnings: list[dict] = []                # {code, message, connector?}
    trace_id: str
    stats: dict = {}                         # {"connector_ms": {...}}
```

Phase 0's `/v1/query` returns this shell: `rows: []`, populated `trace_id`, empty metadata.

### Error vocabulary — `src/models/errors.py`

Six codes, one enum, used everywhere. The first four are query-execution; the last two are gateway.

| Code | HTTP | Note |
|---|---|---|
| `RATE_LIMIT_EXHAUSTED` | 429 | **+ `Retry-After` header**; `suggested_action` must name the async reroute |
| `STALE_DATA` | 200 | a warning inside the envelope, not an error response |
| `ENTITLEMENT_DENIED` | 403 | an **explicit** `deny` only; default-deny yields **empty**, not 403 |
| `SOURCE_TIMEOUT` | 200 partial / 504 | |
| `CONNECTOR_NOT_ENABLED` | 403 | gateway |
| `CONNECTOR_AUTH_ERROR` | 403 | gateway |

A plain auth failure is **401 `UNAUTHENTICATED`** — not one of the six domain codes.

A single FastAPI exception handler maps `ApiError` → `{error_code, message, retry_after_ms?,
suggested_action?}` and sets `Retry-After` when present.

### JWT claims — `src/gateway/auth.py`

HS256, secret from `JWT_SECRET`. Claims shape matches OIDC so the mock is swappable:

```json
{ "sub": "alice", "roles": ["support"], "tenant_id": "tenant_acme",
  "aud": "ema-universal-sql", "exp": "<now+1h>" }
```

`AuthContextExtractor.extract()` verifies signature, `exp` and `aud` → `UserContext {tenant_id, user_id,
roles, raw_claims}`. Failure → 401.

### Control-plane reads — `src/control_plane/repository.py`

Every method **TTL-cached in process** (`CONTROL_PLANE_TTL_MS`, default 30 000), with an explicit
`invalidate()` used by `/v1/test/reset`.

| Method | Read by | Needed in |
|---|---|---|
| `get_tenant(tenant_id)` → status, residency, fernet_key | the tenant-status gate | **P0** |
| `get_rate_limit_policy(tenant_id, connector_type)` | `TokenBucketRateLimiter` sizing | P1 |
| `get_tenant_connectors(tenant_id)` → enabled + secret_ref | `CONNECTOR_NOT_ENABLED` gate | P2 |
| `get_capabilities(connector_type)` | `QueryPlanner` capability check | P2 |
| `get_policies(tenant_id, connectors, resources, roles)` | `EntitlementEngine` | P2 |

Phase 0 implements **all five** (they are one query each) but only the tenant gate consumes one.

### Postgres schema — `migrations/001_init.sql`

Seven tables, mirroring design-doc §8.3: `tenants` (status/residency/`deployment_mode`/`fernet_key`) ·
`connectors` (global catalog: `version`, `capabilities` JSONB) · `tenant_connector` (grant: `enabled`,
`status`, `secret_ref`) · `secrets` (`secret_ref` PK, `ciphertext`) · `policies` (`kind` RLS|CLS, `applies_to`,
`effect`, `predicate` JSONB, `column_name`, `mask`, `version`, `enabled`, + index on
`(tenant_id, connector_type, resource, enabled)`) · `rate_limit_policies` (`max_requests`, `window_sec`,
`burst`) · `audit_logs`. **Empty this phase** — seeded in P1/P2. Full DDL is in the phase file.

## Routes

| Method | Path | Behaviour this phase | Tier |
|---|---|---|---|
| `GET` | `/healthz` | 200 `{status:"ok"}` once pg + redis reachable | MUST |
| `POST` | `/v1/auth/mock-token` | `{user, role, tenant}` → `{token}` | MUST |
| `POST` | `/v1/query` | JWT → 401 if bad; tenant gate; deadline + `trace_id`; return envelope **shell** | MUST |
| `GET` | `/metrics` | our route, `generate_latest(REGISTRY)` (ADR-015) | MUST |
| `POST` | `/v1/query/async` | **501** stub — so the 429 `suggested_action` pointer is not a 404 | MUST |
| `POST` | `/v1/test/reset` | test-only, `TEST_MODE=1`: flush Redis + re-seed + `repository.invalidate()` | SHOULD |

`/v1/query` attaches a per-request **deadline** (`REQUEST_TIMEOUT_MS`, default 5s) to the request context. It
bounds the whole pipeline and is the parent of the per-source budgets added in P1/P2 — a source that blows its
slice degrades to `partial` rather than hanging the request (brief line 84).

## Tasks

Tiers from `02-DEFINITION-OF-DONE.md` §3. Everything MUST unless noted.

| # | Task | Tier | Files |
|---|---|---|---|
| T0 | AST spike ✅ done | MUST | `spike/ast_spike.py` |
| T1 | `pyproject.toml` + deps (incl. the two OTel/Prom corrections) | MUST | `pyproject.toml` |
| T2 | `Dockerfile` (python:3.11-slim) + `docker-compose.yml`; app waits on pg+redis healthy | MUST | `Dockerfile`, `docker-compose.yml` |
| T3 | `Makefile`: `up down seed test e2e load demo fmt` — `demo` declared now, filled in P2 | MUST | `Makefile` |
| T4 | Contract models: `UserContext`, `QueryRequest`, envelope set | MUST | `src/models/{context,request,envelope}.py` |
| T5 | `ErrorCode` + `ApiError` + the single exception handler | MUST | `src/models/errors.py`, `src/gateway/handlers.py` |
| T6 | `mint_mock_token`, `AuthContextExtractor`, `get_current_user`, **tenant-status gate** | MUST | `src/gateway/{auth,deps}.py` |
| T7 | psycopg pool + migration runner + `001_init.sql` | MUST | `src/control_plane/db.py`, `migrations/001_init.sql` |
| T8 | `repository.py` — 5 reads, TTL cache, `invalidate()` | MUST | `src/control_plane/repository.py` |
| T9 | `@stage_span` decorator, tracer provider, Prometheus registry + `rate_limit_remaining` gauge | MUST | `src/observability/{tracing,metrics}.py` |
| T10 | App factory + the six routes | MUST | `src/main.py` |
| T11 | `README.md` skeleton, **Quickstart section filled** | MUST | `README.md` |
| T12 | Tests (below) + promote the spike | MUST | `tests/**` |

**`/v1/test/reset` (part of T10) is SHOULD** — it exists so Phase 3's Playwright `beforeEach` and Phase 4's
k6 runs are deterministic. Cheap now, retrofit-expensive later.

## Tests

`tests/unit/` must stay infra-free — the commit hook runs `pytest -q tests/unit` on every `git commit` and
will block on failure. Anything needing containers goes in `tests/integration/`.

| File | Asserts |
|---|---|
| `tests/unit/test_auth.py` | valid token → `UserContext` with right tenant/roles; **expired → 401**; **wrong `aud` → 401**; bad signature → 401 |
| `tests/unit/test_control_plane.py` | a second call inside the TTL **does not hit Postgres** (assert the query count); `invalidate()` forces a re-read |
| `tests/unit/test_models.py` | `QueryEnvelope` shell round-trips; `next_cursor` is `None` when `partial=True`; the six `ErrorCode` values and their HTTP mappings |
| `tests/unit/test_observability.py` | `@stage_span` records a named span; `trace_id` is 32 hex and shared parent→child; the `rate_limit_remaining` gauge appears in `generate_latest(REGISTRY)` alongside a golden-signal family |
| `tests/unit/test_ast_spike.py` | the promoted spike — predicate split, RLS row counts 3/1/0, CLS digest with no `@`, cross-source predicate stays residual |
| `tests/integration/test_scaffold.py` | `/healthz` 200; `/v1/query` **no token → 401**; with a minted token → **200 envelope shell with non-empty `trace_id`**; `/v1/query/async` → 501; `/metrics` exposes both metric families |

**Manual gate:** `make up` cold → `/healthz` green in **< 60s** (submission gate 3).

## README

Phase 0 creates `README.md` with **all** section headings stubbed, and fills exactly one. Filling your section
is part of each phase's gate — a README written only at the end describes what you meant to build, and a
Phase 4 slip would lose a required deliverable outright (it is brief deliverable #4, line 16).

| Section | Filled by |
|---|---|
| **Quickstart (`make up && make seed`)** | **P0 — this phase** |
| What it proves — five hard parts, each with its command | P2 |
| Trade-offs + join strategy (federated vs materialised) | P2 |
| Prod-mapping of every non-goal (HLD §7) | P4 |
| Screenshots + what they prove | P4 |
| Access granted to `souvik-sen@` / `careers@` | P4 |

The Quickstart must also carry **one line stating what this repo is**: deliverables 3 and 4 (prototype +
README); the design doc and six-month plan are the Google Doc, and `docs/design/design-doc.md` is a reference
copy. Without it a reviewer reads `docs/design/` as the submission.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Hooks block the build: files >500 lines block commits; adding `ignore`/`noqa`/`disable` to a config is blocked; `ruff format` runs on every `.py` edit | Keep every module small by construction — the file map above is already decomposed. Creating `pyproject.toml` is allowed; weakening it is not |
| `python` is not on PATH (only `python3`) — this silently no-op'd a previous commit gate | Use `.venv/bin/python`; the venv is pinned to **3.11.15** via `uv` |
| Cold start exceeds the 60s submission gate | 3 services only (ADR-016); slim base image; timed on a fresh clone in P4 |
| Envelope contract churns after later phases start filling it | It is frozen at T4 and is a locked rail (HLD §9). A change means changing the HLD, the phase specs *and* the submitted design doc together |
| Instrumentation packages are pre-1.0 | Pin as a set; `test_observability.py` fails loudly if the shared-registry assumption breaks |

## Done when

All Phase 0 tests green, `make up` healthy, `/healthz` green in under 60s cold, 401 on bad token / 200 envelope
shell on good token, and **the envelope + error models are importable and used by the route** even though
execution is still a shell.

## Source artifacts

- Brief: `docs/design/take_home.md` · HLD: `docs/design/00-PROTOTYPE-HLD.md`
- Phase file (**wins on conflict**): `docs/design/phases/phase-0-scaffold.md`
- Tiers / submission gate: `docs/design/02-DEFINITION-OF-DONE.md` §3
- ADRs: `docs/kickoff/v1/architecture.md` · Research: `docs/kickoff/v1/research-repos.md`
- Build loop: `docs/design/03-BUILD-PROCESS.md`
