# Phase 0 — Scaffold & Contracts

> **Goal:** an end-to-end skeleton — containers up, auth working, the response contract typed — so every later
> phase fills a contract that already exists. **Gate:** `make up` healthy; 401 on bad token, 200 shell on good token.
> Build into the repo layout in [`../00-PROTOTYPE-HLD.md`](../00-PROTOTYPE-HLD.md) §8.

## Task 0 — the de-risking spike *(do this FIRST, before any infra)*
The intellectual core of the whole prototype is Phase 2's AST work, and as written it sits behind ~4 hours of
plumbing. Prove it in **60–90 minutes**, in one throwaway script (`spike/ast_spike.py`), with **no** FastAPI,
Postgres, Redis, or Docker — hardcoded Python dicts for the two datasets:

1. `sqlglot.parse_one(CANONICAL_SQL, read="duckdb")` → `qualify(tree, schema=...)`
2. AND an RLS `exp.EQ` into the WHERE via `tree.where(..., append=True)`
3. Rewrite one projection node to `MD5(reporter_email) AS reporter_email`
4. Flatten the top-level `exp.And` and group leaf predicates by owning table
5. `duckdb.register()` two `pyarrow.Table`s and run the residual join over them

**Gate:** the canonical query returns joined rows, and the printed per-source predicate split is correct.
**If this does not work, the plan changes** — better to learn it now than at hour 5. Delete the spike (or keep
it as `tests/unit/test_ast_spike.py`) once Phase 2 lands the real thing.

## Deliverables
1. `docker-compose.yml` + `Dockerfile` (for `app`) — services `app` (FastAPI), `postgres:16`, `redis:7`; `app` waits for pg+redis healthy. The `Dockerfile` is what `docker-compose build` uses; keep it slim (python:3.11-slim + `pip install .`).
2. `Makefile` — `up` (compose up + wait), `down`, `seed`, `test`, `e2e`, `load`, `demo`, `fmt`. (`demo` is filled in at Phase 2; declare the target now so it is never forgotten.)
3. `pyproject.toml` — deps: `fastapi`, `uvicorn`, `pydantic>=2`, `pyjwt`, `cryptography` (Fernet), `sqlglot`, `duckdb`, `pyarrow` (DuckDB `register`/`fetch_arrow_table`), `redis`, `psycopg[binary]`, `opentelemetry-sdk`, `prometheus-client`; dev: `pytest`, `pytest-asyncio`, `httpx`, `playwright`.
4. FastAPI app factory (`src/main.py`) mounting the routes below.
5. The **contract models** (`src/models/`) — the single source of truth for request/response/errors.
6. `AuthContextExtractor` + `/v1/auth/mock-token`.
7. Postgres migration runner + empty-schema migration (tables defined here, seeded in Phase 1/2).
8. **`src/control_plane/repository.py` — the runtime read layer.** `src/control_plane/` is in the HLD §8 layout
   and three later steps depend on it, but no phase owned it. It belongs here, with Phase 0's tenant gate as its
   first consumer:

   | Method | Read by | Phase |
   |---|---|---|
   | `get_tenant(tenant_id)` → status, residency, fernet_key | the tenant-status gate below | P0 |
   | `get_tenant_connectors(tenant_id)` → enabled + secret_ref | `CONNECTOR_NOT_ENABLED` coarse gate | P2 |
   | `get_capabilities(connector_type)` | `QueryPlanner` capability check | P2 |
   | `get_policies(tenant_id, connectors, resources, roles)` | `EntitlementEngine` | P2 |
   | `get_rate_limit_policy(tenant_id, connector_type)` | `TokenBucketRateLimiter` sizing | P1 |

   Every method is **TTL-cached in process** (`CONTROL_PLANE_TTL_MS`, default 30 000) with an explicit
   `invalidate()` used by `/v1/test/reset`. This is what makes HLD §3's *"read at request time (cached)"* true
   rather than aspirational — without it every query pays 4–5 Postgres round-trips and the Phase-4 P95 is
   Postgres, not Jira.

## Routes (this phase)
| Method | Path | Behavior this phase |
|---|---|---|
| `GET` | `/healthz` | 200 `{status:"ok"}` once pg+redis reachable |
| `POST` | `/v1/auth/mock-token` | body `{user, role, tenant}` → `{token}` (HS256, `JWT_SECRET` from env) |
| `POST` | `/v1/query` | validate JWT → 401 if bad; else return the 200 envelope **shell** (`rows:[]`, populated `trace_id`, empty metadata). No real execution yet. |
| `GET` | `/metrics` | Prometheus endpoint stub (fleshed out Phase 4) |
| `POST` | `/v1/query/async` | **501 Not Implemented** stub — the async reroute is a documented non-goal (HLD §7); this exists so the 429 `suggested_action` pointer isn't a 404 |
| `POST` | `/v1/test/reset` | test-only (guarded by `TEST_MODE=1`): flush Redis buckets + re-seed → deterministic Playwright/k6 runs (used in Phase 3/4) |

**Request shaping (gateway).** `/v1/query` attaches a **per-request deadline** (`REQUEST_TIMEOUT_MS`, default 5s) and the `trace_id` to the request context; the deadline bounds the whole pipeline and is the parent of the per-source budgets (Phase 1/2) — a source that blows its slice degrades to `partial` rather than hanging the request (take-home line 84 "timeouts"). Subset validation (reject writes/DDL/functions) is Phase 2's `SQLParser`.

## Contract models (`src/models/`) — build these exactly

```python
# models/context.py
@dataclass(frozen=True)
class UserContext:
    tenant_id: str
    user_id: str            # == jwt "sub"
    roles: list[str]        # from jwt "roles"/"groups"
    raw_claims: dict

# models/request.py  (Pydantic)
class QueryRequest(BaseModel):
    sql: str
    max_staleness_ms: int = 60_000
    cursor: str | None = None       # echo the previous response's next_cursor to page the joined result

# models/envelope.py  (Pydantic) — THE response contract (HLD §4)
class ColumnMeta(BaseModel):
    name: str; type: str; source: str; masked: bool = False
class ConnectorBudget(BaseModel):
    remaining: int; throttled: bool
class SourceOutcome(BaseModel):
    connector: str
    state: Literal["ok","timeout","error","throttled"]
    served: Literal["live","cache","none"]
class QueryEnvelope(BaseModel):
    columns: list[ColumnMeta] = []
    rows: list[list] = []
    freshness_ms: int | None = None
    rate_limit_status: dict[str, ConnectorBudget] = {}
    sources: list[SourceOutcome] = []
    join_status: Literal["complete","incomplete","n/a"] = "n/a"
    partial: bool = False
    next_cursor: str | None = None     # opaque offset over the joined result; null when exhausted
    warnings: list[dict] = []          # {code, message, connector?}
    trace_id: str
    stats: dict = {}                   # {"connector_ms": {...}}

# models/errors.py — the SIX-code vocabulary (HLD §4). One enum, used everywhere.
class ErrorCode(str, Enum):
    RATE_LIMIT_EXHAUSTED="RATE_LIMIT_EXHAUSTED"   # 429  +Retry-After
    STALE_DATA="STALE_DATA"                        # 200 warning
    ENTITLEMENT_DENIED="ENTITLEMENT_DENIED"        # 403
    SOURCE_TIMEOUT="SOURCE_TIMEOUT"                # 200 partial / 504
    CONNECTOR_NOT_ENABLED="CONNECTOR_NOT_ENABLED"  # 403 gateway
    CONNECTOR_AUTH_ERROR="CONNECTOR_AUTH_ERROR"    # 403 gateway
class ApiError(Exception):
    code: ErrorCode; http: int; message: str; retry_after_ms: int | None = None
```

A single FastAPI exception handler maps `ApiError` → the right HTTP status + `{error_code, message, retry_after_ms?, suggested_action?}` and sets the `Retry-After` header when present. For `RATE_LIMIT_EXHAUSTED`, `suggested_action` **names the async reroute** (e.g. *"retry after 47s, or run as an async job via POST /v1/query/async"*) — the async path itself is a documented non-goal (HLD §7), but the friendly error must point at it (take-home lines 110, 158).

## Auth (`src/gateway/auth.py`)
- `mint_mock_token(user, role, tenant) -> str`: HS256, claims `{sub:user, roles:[role], tenant_id:tenant, aud:"ema-universal-sql", exp:+1h}`.
- `AuthContextExtractor.extract(authorization_header) -> UserContext`: decode, verify signature + `exp` + `aud`; raise `ApiError(ENTITLEMENT_DENIED?/401)` → actually a 401 `UNAUTHENTICATED` on failure. (401 is a plain auth failure, not one of the six domain codes.)
- FastAPI dependency `get_current_user` wraps it.
- **Tenant-status gate (crypto-shred front half, design-doc §3.1):** after resolving `tenant_id`, reject if `tenant.status != 'active'` (a `suspended`/`offboarding` tenant → `403 ENTITLEMENT_DENIED`) before any planning. Pairs with the Phase 1 `test_crypto_shred` (key-destroy back half).

## Postgres schema (migration `001_init.sql`) — mirrors design-doc §8.3 (NOT the raw-SQL variant)
```sql
CREATE TABLE tenants (
  tenant_id TEXT PRIMARY KEY, name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',        -- active|suspended|offboarding
  residency TEXT NOT NULL DEFAULT 'us',
  deployment_mode TEXT NOT NULL DEFAULT 'multi-tenant',
  fernet_key TEXT NOT NULL);                     -- per-tenant key (stands in for KMS)
CREATE TABLE connectors (                         -- GLOBAL catalog (data-not-code); NOT per-tenant
  connector_type TEXT PRIMARY KEY,               -- 'github' | 'jira'
  version TEXT NOT NULL, capabilities JSONB NOT NULL);
CREATE TABLE tenant_connector (                   -- per-tenant GRANT
  tenant_id TEXT REFERENCES tenants, connector_type TEXT REFERENCES connectors,
  enabled BOOLEAN NOT NULL DEFAULT true, status TEXT NOT NULL DEFAULT 'active',
  secret_ref TEXT NOT NULL,                       -- pointer; encrypted secret stored in `secrets`
  PRIMARY KEY (tenant_id, connector_type));
CREATE TABLE secrets (                            -- Fernet-encrypted; resolved by secret_ref
  secret_ref TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, ciphertext TEXT NOT NULL);
CREATE TABLE policies (
  policy_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
  connector_type TEXT NOT NULL, resource TEXT NOT NULL,
  kind TEXT NOT NULL,                             -- 'RLS' | 'CLS'
  applies_to TEXT NOT NULL,                       -- role name or '*'
  effect TEXT NOT NULL DEFAULT 'allow',           -- 'allow' | 'deny'
  predicate JSONB,                                -- RLS: filter AST
  column_name TEXT, mask TEXT,                     -- CLS: null|hash|redact|drop
  version INT NOT NULL DEFAULT 1, enabled BOOLEAN NOT NULL DEFAULT true);
CREATE INDEX ON policies (tenant_id, connector_type, resource, enabled);
CREATE TABLE rate_limit_policies (
  tenant_id TEXT, connector_type TEXT,
  max_requests INT NOT NULL, window_sec INT NOT NULL, burst INT NOT NULL,
  PRIMARY KEY (tenant_id, connector_type));
CREATE TABLE audit_logs (
  log_id BIGSERIAL PRIMARY KEY, tenant_id TEXT, user_id TEXT,
  query_text TEXT, sources_accessed TEXT[], rows_returned INT,
  trace_id TEXT, execution_ms INT, ts TIMESTAMPTZ DEFAULT now());
```

## `README.md` — start it here, grow it every phase
The README is brief deliverable **#4** (line 16) and a submission gate, but as originally planned it was written
once, in Phase 4 — so any slip in the last phase loses a required deliverable outright. Instead, create it in
Phase 0 with the section headings stubbed, and make *filling your section* part of each phase's gate:

| Section | Filled by |
|---|---|
| Quickstart (`make up && make seed`) | P0 |
| What it proves — the five hard parts, each with its command | P2 (after `make demo` exists) |
| Trade-offs + join strategy (federated vs materialised — line 69) | P2 |
| Prod-mapping of every non-goal (HLD §7) | P4 |
| Screenshots + what they prove (line 52) | P4 |
| Access granted to `souvik-sen@` / `careers@` | P4 |

A README that grows with the code is also the only version that stays *true*; one written at the end describes
what you meant to build.

## Acceptance tests (gate)
- `tests/unit/test_control_plane.py`: a second call inside the TTL does **not** hit Postgres (assert the query
  count); `invalidate()` forces a re-read.
- `tests/unit/test_auth.py`: valid token → `UserContext` with right tenant/roles; expired → 401; wrong `aud` → 401.
- `tests/integration/test_scaffold.py`: `/healthz` 200; `/v1/query` no token → 401; with minted token → 200 envelope shell with a non-empty `trace_id`.
- Manual: `make up` cold → `/healthz` green in < 60s.

## Done when
All acceptance tests green, `make up` healthy, and the envelope + error models are importable and used by the route (even though execution is still a shell).
