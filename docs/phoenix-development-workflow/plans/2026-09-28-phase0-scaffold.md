# Phase 0: Scaffold & Contracts

**Goal:** an end-to-end skeleton — containers up, auth working, the response envelope typed — so every later phase fills a contract that already exists.
**Depends on:** none. Task T0 (the AST spike) is **already complete**, 14/14 green; only its promotion to a unit test is planned here.
**Assumes:** nothing from prior phases. What *later* phases assume from this one: `src/models/` is **frozen** after T008 (both P1 and P2 code against it); `pyproject.toml` declares every dependency up front so no later phase edits it; `POST /v1/test/reset` exists for Phase 3's Playwright `beforeEach` and Phase 4's k6 runs.
**Verify:** `make up` → `/healthz` 200 in under 60s cold · `/v1/query` 401 without a token, 200 envelope shell with one · `make test` all green.

> **Hard gate:** this plan is the taste gate. **No code runs until a human approves it** (`docs/design/03-BUILD-PROCESS.md` step 4).
> **Spec:** [`../specs/2026-09-28-phase0-scaffold.md`](../specs/2026-09-28-phase0-scaffold.md) · **Phase file wins on any conflict:** `docs/design/phases/phase-0-scaffold.md`

---

## Constraints this plan is built around

| Constraint | Consequence for the plan |
|---|---|
| Pre-commit hook blocks any file **over 500 lines** (LAW 1: decompose at 400) | No task below is sized to produce more than ~200 lines. `main.py` is split (T017). |
| Pre-commit hook runs `pytest -q tests/unit` | `tests/unit/` must stay **infra-free**. Anything needing Docker lives in `tests/integration/` and is run by `make test-integration`, not the hook. |
| Config-weakening hook blocks `ignore`/`noqa`/`disable`/`strict = false` in configs | `pyproject.toml` is written correct-first (T002). Creating it is allowed; weakening it later is not. |
| `ruff format` runs on every `.py` edit (PostToolUse) | No formatting tasks needed. |
| Tests live in the **same task** as their code (LAW 6) | Every foundational task below carries its own test file. |
| `python` is **not** on PATH — only `python3` | Every verify command uses `.venv/bin/python`. |

**Environment already in place:** `.venv` pinned to **Python 3.11.15** (uv), with sqlglot 30.20.0, duckdb 1.5.5, pyarrow 25.0.1, ruff 0.16.9, fastapi 0.141.1, opentelemetry-sdk 1.45.0, opentelemetry-instrumentation-fastapi 0.66b0, prometheus-client 0.26.0, prometheus-fastapi-instrumentator 8.1.0.
**Docker daemon is currently DOWN** — T001 starts it.

---

## File Map

| Action | Path | Responsibility |
|---|---|---|
| Create | `pyproject.toml` | deps + pytest/ruff config |
| Create | `Dockerfile` | slim app image (python:3.11-slim) |
| Create | `docker-compose.yml` | app · postgres:16 · redis:7 |
| Create | `Makefile` | `up down seed test e2e load demo fmt` |
| Create | `.env.example` | every env var with its default |
| Create | `src/config.py` | typed settings from env |
| Create | `src/models/context.py` | `UserContext` |
| Create | `src/models/request.py` | `QueryRequest` |
| Create | `src/models/envelope.py` | `QueryEnvelope` + 3 sub-models |
| Create | `src/models/errors.py` | `ErrorCode` + `ApiError` |
| Create | `src/gateway/handlers.py` | `ApiError` → HTTP exception handler |
| Create | `src/gateway/auth.py` | `mint_mock_token`, `AuthContextExtractor` |
| Create | `src/gateway/deps.py` | `get_current_user` + tenant-status gate |
| Create | `src/gateway/routes.py` | the six routes |
| Create | `src/control_plane/db.py` | psycopg pool + migration runner |
| Create | `src/control_plane/repository.py` | 5 TTL-cached control-plane reads |
| Create | `src/observability/tracing.py` | tracer provider + `@stage_span` |
| Create | `src/observability/metrics.py` | registry + `rate_limit_remaining` gauge |
| Create | `src/main.py` | app factory + lifespan + router mount |
| Create | `migrations/001_init.sql` | 7 tables, empty |
| Create | `tests/unit/test_models.py` `test_errors.py` `test_auth.py` `test_control_plane.py` `test_observability.py` `test_ast_spike.py` | unit gate |
| Create | `tests/integration/test_scaffold.py` | route gate |
| Create | `tests/conftest.py` | shared fixtures |
| Create | `README.md` | Quickstart filled; other headings stubbed |
| Modify | `.gitignore` | add `.env` if absent |
| Delete | `spike/ast_spike.py` | after promotion in T020 |

---

## Phase: Setup

### Task T001: Verify the toolchain and start Docker *(Step 0 — inspect before building)*
**Files:** none (exploratory)
- [x] Start Docker Desktop; wait for the daemon
- [x] Confirm `.venv/bin/python -V` reports 3.11.15
- [x] Confirm `docker compose version` and that `docker info` succeeds
- [x] Pull `postgres:16` and `redis:7` so T003's first `make up` isn't also a cold image pull
- [x] Verify: `docker info >/dev/null && .venv/bin/python -V` → exits 0, prints `Python 3.11.15`

### Task T002: `pyproject.toml`
**Files:** create `pyproject.toml`
**Decision:** declare **every** dependency now — including Phase 1–4's (`redis`, `psycopg[binary]`, `cryptography`, `sqlglot`, `duckdb`, `pyarrow`) — because `03-BUILD-PROCESS.md` makes "no later phase edits `pyproject.toml`" a precondition for the phases being file-disjoint.
**Decision:** add `opentelemetry-instrumentation-fastapi` and `prometheus-fastapi-instrumentator`, which the phase file's list omits — `FastAPIInstrumentor` ships in its own distribution (v1 research). Pin the four `opentelemetry-*` packages as a set; they are pre-1.0 and must move together.
- [x] Write `[project]` with name, Python `>=3.11,<3.12`, and the runtime deps
- [x] Add `[project.optional-dependencies] dev` = pytest, pytest-asyncio, httpx, playwright, ruff
- [x] Add `[tool.pytest.ini_options]` with `testpaths`, `asyncio_mode = "auto"`
- [x] Add `[tool.ruff]` line-length and lint selection — **no ignore list** (the hook blocks it, and LAW 7 says fix the code)
- [x] Verify: `VIRTUAL_ENV=.venv uv pip install -e ".[dev]"` succeeds and `.venv/bin/python -c "import fastapi, sqlglot, duckdb, redis, psycopg, cryptography"` exits 0

### Task T003: `Dockerfile` + `docker-compose.yml`
**Files:** create `Dockerfile`, `docker-compose.yml`, `.env.example`; modify `.gitignore`
**Decision:** **three services only** — app, postgres:16, redis:7. No Jaeger/OTLP container (ADR-016), because the submission gate times cold-to-serving at under 60s and spans carry the waterfall data without a backend.
**Decision:** app `depends_on` both with `condition: service_healthy`, using `pg_isready` and `redis-cli ping` healthchecks, so `make up` can't race the DB.
- [x] Write the `Dockerfile`: `python:3.11-slim`, copy `pyproject.toml`, `pip install .`, copy `src/` + `migrations/`, `CMD uvicorn src.main:app`
- [x] Write `docker-compose.yml` with the three services, healthchecks, and env wiring
- [x] Write `.env.example` listing every variable with its default: `JWT_SECRET`, `JWT_AUDIENCE`, `DATABASE_URL`, `REDIS_URL`, `REQUEST_TIMEOUT_MS=5000`, `CONTROL_PLANE_TTL_MS=30000`, `CACHE_TTL_MS`, `TEST_MODE=0`
- [x] Ensure `.gitignore` covers `.env`
- [x] Verify: `docker compose config` parses and lists exactly 3 services

### Task T004: `Makefile`
**Files:** create `Makefile`
**Decision:** declare `demo` **now** as a stub that exits with a "filled in Phase 2" message, because the phase file warns a target introduced late is a target forgotten.
- [x] Targets: `up` (compose up -d + wait for `/healthz`), `down`, `seed`, `test`, `test-integration`, `e2e`, `load`, `demo`, `fmt`
- [x] `test` runs `.venv/bin/python -m pytest -q tests/unit`; `test-integration` runs `tests/integration` (kept separate — the commit hook only runs unit)
- [x] Verify: `make -n up test fmt` prints the commands without executing

---

## Phase: Foundational *(blocking — nothing below starts until these land)*

### Task T005: `src/config.py` — typed settings
**Files:** create `src/config.py`, `src/__init__.py`, `tests/unit/test_config.py`
**Decision:** a Pydantic `BaseSettings`-style object read once at import, rather than scattered `os.getenv` calls — every later phase reads `CACHE_TTL_MS`, `REQUEST_TIMEOUT_MS` and `CONTROL_PLANE_TTL_MS`, and a typo in a raw getenv is a silent default. *(Not in the HLD §8 tree; added because five modules need it.)*
- [x] Define `Settings` with the eight variables from `.env.example` and their defaults
- [x] Write `tests/unit/test_config.py`: defaults apply when env is empty; an env override wins; `TEST_MODE` coerces to bool
- [x] Verify: `.venv/bin/python -m pytest -q tests/unit/test_config.py`

### Task T006: contract models — context + request
**Files:** create `src/models/__init__.py`, `src/models/context.py`, `src/models/request.py`
- [x] `UserContext` as a frozen dataclass: `tenant_id`, `user_id`, `roles`, `raw_claims`
- [x] `QueryRequest` (Pydantic): `sql`, `max_staleness_ms: int = 60_000`, `cursor: str | None = None`
- [x] Verify: `.venv/bin/python -c "from src.models.context import UserContext; from src.models.request import QueryRequest; print(QueryRequest(sql='SELECT 1').max_staleness_ms)"` prints `60000`

### Task T007: the response envelope
**Files:** create `src/models/envelope.py`, `tests/unit/test_models.py`
**Decision:** transcribe HLD §4 **verbatim** — `ColumnMeta`, `ConnectorBudget`, `SourceOutcome`, `QueryEnvelope` — because the envelope is a locked provenance rail (HLD §9) shared with the submitted design doc.
- [x] Write the four models with the exact field names, types and defaults from the spec
- [x] Write `tests/unit/test_models.py`: an envelope shell serialises with `rows: []` and a `trace_id`; `join_status` defaults to `"n/a"`; a `Literal` violation on `SourceOutcome.state` raises
- [x] Add the test asserting `next_cursor is None` whenever `partial is True` (HLD §9 rail)
- [x] Verify: `.venv/bin/python -m pytest -q tests/unit/test_models.py`

### Task T008: error vocabulary + the exception handler — **`src/models/` freezes here**
**Files:** create `src/models/errors.py`, `src/gateway/__init__.py`, `src/gateway/handlers.py`, `tests/unit/test_errors.py`
**Decision:** a plain auth failure is **401 `UNAUTHENTICATED`**, deliberately *not* one of the six domain codes — the six are the shared vocabulary between the prototype and design-doc §8.1, and diluting them with transport-level failures desyncs the two deliverables.
- [x] `ErrorCode` str-Enum with exactly the six codes; `ApiError` exception carrying `code`, `http`, `message`, `retry_after_ms`
- [x] `install_error_handlers(app)`: map `ApiError` → `{error_code, message, retry_after_ms?, suggested_action?}`, set the `Retry-After` header when `retry_after_ms` is present
- [x] For `RATE_LIMIT_EXHAUSTED`, `suggested_action` must **name the async reroute** (brief lines 110/158)
- [x] Write `tests/unit/test_errors.py`: all six codes present; the 429 path sets `Retry-After` and a `suggested_action` mentioning `/v1/query/async`; 401 is not in `ErrorCode`
- [x] Verify: `.venv/bin/python -m pytest -q tests/unit/test_errors.py`

### Task T009: OTel tracing + the `@stage_span` decorator
**Files:** create `src/observability/__init__.py`, `src/observability/tracing.py`, `tests/unit/test_observability.py`
**Decision:** `@stage_span` is a **thin wrapper over `tracer.start_as_current_span`**, not custom machinery — runtime-verified this session that the first-party API already works as a bare decorator, auto-parents nested spans, and carries durations.
**Decision:** `trace_id` is read from the **ambient span** via `format(ctx.trace_id, "032x")`, not generated as a separate UUID, so `QueryEnvelope.trace_id` actually resolves against the exported trace.
- [x] Build the `TracerProvider` with a console/in-memory exporter (ADR-016 — no OTLP)
- [x] `stage_span(name)` decorator that also records elapsed ms for `stats.connector_ms`
- [x] `current_trace_id()` helper returning the 32-hex string
- [x] Write `tests/unit/test_observability.py` (tracing half) against an `InMemorySpanExporter`: the decorator records a named span; parent and child share one `trace_id`; `current_trace_id()` is 32 hex chars
- [x] Verify: `.venv/bin/python -m pytest -q tests/unit/test_observability.py -k tracing`

### Task T010: Prometheus registry + the connector gauge
**Files:** create `src/observability/metrics.py`; extend `tests/unit/test_observability.py`
**Decision:** **one `/metrics` route we own** — `Instrumentator().instrument(app)` for the golden-signal collectors but **not** `.expose(app)` (ADR-015). Runtime-verified the library defaults to `prometheus_client`'s shared `REGISTRY`, so our gauge and its histograms land in the same scrape.
- [x] Declare `rate_limit_remaining` Gauge with labels `connector`, `tenant`
- [x] `render_metrics()` returning `generate_latest(REGISTRY)` + `CONTENT_TYPE_LATEST`
- [x] `instrument_app(app)` calling `.instrument(app)` only
- [x] Extend the test: the gauge appears in `generate_latest(REGISTRY)` with both labels, **alongside** a golden-signal family — this is the assertion that fails loudly if a future version splits the registry
- [x] Verify: `.venv/bin/python -m pytest -q tests/unit/test_observability.py`

### Task T011: Postgres schema + migration runner
**Files:** create `migrations/001_init.sql`, `src/control_plane/__init__.py`, `src/control_plane/db.py`
**Decision:** transcribe the DDL from the phase file exactly (design-doc §8.3 shape, **not** the raw-SQL policy variant) — `connectors` is a *global* catalog and `tenant_connector` is the per-tenant grant (ADR-013).
**Decision:** the runner is idempotent (tracks applied files in a `schema_migrations` table) so `make up` is safe to re-run.
- [x] Write the 7 `CREATE TABLE` statements + the `policies` index; tables stay **empty** this phase
- [x] Write the psycopg connection pool and `run_migrations()`
- [x] Verify: `make up && docker compose exec -T postgres psql -U postgres -c '\dt'` lists all 7 tables plus `schema_migrations`

### Task T012: control-plane repository with a TTL cache
**Files:** create `src/control_plane/repository.py`, `tests/unit/test_control_plane.py`
**Decision:** implement **all five** reads now even though only `get_tenant` has a consumer this phase — they are one query each, and the phase file assigns the whole read layer here precisely because three later phases depend on it and no phase owned it.
**Decision:** cache is in-process with a monotonic-clock TTL (`CONTROL_PLANE_TTL_MS`, default 30 000) and an explicit `invalidate()`. Without it every query pays 4–5 round-trips and Phase 4's P95 measures Postgres, not Jira.
- [x] Implement `get_tenant`, `get_tenant_connectors`, `get_capabilities`, `get_policies`, `get_rate_limit_policy`
- [x] Implement the TTL cache wrapper + `invalidate()`
- [x] Write `tests/unit/test_control_plane.py` against a **fake connection that counts queries** (keeps it infra-free for the commit hook): a second call inside the TTL does **not** hit Postgres; after `invalidate()` it does; expiry past the TTL re-reads
- [x] Verify: `.venv/bin/python -m pytest -q tests/unit/test_control_plane.py`

### Task T013: JWT minting + extraction
**Files:** create `src/gateway/auth.py`, `tests/unit/test_auth.py`
**Decision:** HS256 with claims shaped like OIDC (`sub`, `roles`, `tenant_id`, `aud`, `exp`) so the mock is swappable for a real IdP without changing `UserContext`.
- [x] `mint_mock_token(user, role, tenant)` → signed JWT, `exp` +1h, `aud = "ema-universal-sql"`
- [x] `AuthContextExtractor.extract(authorization_header)` → `UserContext`; verify signature, `exp` **and** `aud`; raise 401 on any failure
- [x] Write `tests/unit/test_auth.py`: valid → right tenant/roles; **expired → 401**; **wrong `aud` → 401**; bad signature → 401; missing/garbled header → 401
- [x] Verify: `.venv/bin/python -m pytest -q tests/unit/test_auth.py`

### Task T014: `get_current_user` dependency + the tenant-status gate
**Files:** create `src/gateway/deps.py`; extend `tests/unit/test_auth.py`
**Decision:** the tenant-status gate (reject `status != 'active'` → `403 ENTITLEMENT_DENIED`) runs **before any planning** — it is the front half of crypto-shred (design-doc §3.1) and pairs with Phase 1's `test_crypto_shred`.
- [x] `get_current_user` FastAPI dependency wrapping `AuthContextExtractor`
- [x] After resolving `tenant_id`, read `repository.get_tenant()` and reject a suspended/offboarding tenant
- [x] Extend the test: an `active` tenant passes; a `suspended` tenant → 403 `ENTITLEMENT_DENIED` (not 401 — it authenticated fine)
- [x] Verify: `.venv/bin/python -m pytest -q tests/unit/test_auth.py`

---

## Phase: Routes

### Task T015: app factory + `/healthz` + `/metrics`
**Files:** create `src/main.py`
**Decision:** split the app factory (`main.py`) from the route definitions (`gateway/routes.py`, T016–T017). HLD §8 comments `main.py` as "factory + routes", but the route-handler limit is 80 lines (LAW 1) and P2/P3/P4 all rewire routes — keeping them in `src/gateway/` matches §8's own directory intent and reduces churn on the one shared file.
**Decision:** create the psycopg pool and the Redis client in the **lifespan** and hang them on `app.state`, so Phase 1's governance modules take the same clients rather than opening their own.
- [x] `create_app()`: lifespan (pool + redis + `run_migrations()`), `install_error_handlers`, `FastAPIInstrumentor.instrument_app`, `instrument_app` (Prometheus), mount the router
- [x] `GET /healthz` → 200 `{status:"ok"}` only when **both** pg and redis answer; 503 otherwise
- [x] `GET /metrics` → `render_metrics()`
- [x] Verify: `make up && curl -fsS localhost:8000/healthz` → `{"status":"ok"}`; `curl -s localhost:8000/metrics | grep -c rate_limit_remaining` ≥ 1

### Task T016: `/v1/auth/mock-token` and `/v1/query` shell
**Files:** create `src/gateway/routes.py`
**Decision:** `/v1/query` returns the **typed `QueryEnvelope`**, not an ad-hoc dict, even though it is empty — the point of the phase is that the contract exists and the route already uses it.
- [x] `POST /v1/auth/mock-token` `{user, role, tenant}` → `{token}`
- [x] `POST /v1/query`: `get_current_user` → 401 on bad token; attach the `REQUEST_TIMEOUT_MS` deadline and `trace_id` to the request context; return the envelope shell with a populated `trace_id`
- [x] Verify: mint a token via curl, then `curl -s -X POST localhost:8000/v1/query -H "Authorization: Bearer $T" -d '{"sql":"SELECT 1"}' | jq -e '.trace_id != null and (.rows | length) == 0'`

### Task T017: `/v1/query/async` stub + `/v1/test/reset`
**Files:** modify `src/gateway/routes.py`
**Decision:** `/v1/query/async` returns **501, not 404** — the 429 `suggested_action` points at it, and a pointer to a 404 reads as a bug rather than a documented non-goal (HLD §7).
**Decision:** `/v1/test/reset` is SHOULD-tier but built now: Phase 3's Playwright `beforeEach` and Phase 4's k6 both need a deterministic start, and retrofitting it means re-touching this file twice more.
- [x] `POST /v1/query/async` → 501 with a body naming the non-goal
- [x] `POST /v1/test/reset` → guarded by `TEST_MODE=1` (404 otherwise): flush Redis, re-run seed, call `repository.invalidate()`
- [x] Verify: `curl -s -o /dev/null -w '%{http_code}' -X POST localhost:8000/v1/query/async` → `501`; reset returns 404 with `TEST_MODE=0`

### Task T018: integration test for the scaffold gate
**Files:** create `tests/conftest.py`, `tests/integration/__init__.py`, `tests/integration/test_scaffold.py`
**Decision:** lives in `tests/integration/` — it needs live pg + redis, and the pre-commit hook runs only `tests/unit`, so putting it there would make every commit require Docker.
- [x] Fixtures: an httpx client against the running app; a minted-token helper
- [x] Assert `/healthz` → 200
- [x] Assert `/v1/query` **no token → 401**
- [x] Assert `/v1/query` **with a minted token → 200**, body validates as `QueryEnvelope`, `trace_id` non-empty
- [x] Assert `/v1/query/async` → 501; `/metrics` exposes **both** a golden-signal family and `rate_limit_remaining`
- [x] Verify: `make test-integration`

---

## Phase: Polish

### Task T019: promote the AST spike to a unit test
**Files:** create `tests/unit/test_ast_spike.py`; delete `spike/ast_spike.py`
**Decision:** promote rather than delete. The bug it caught (a single `flatten()` silently yielding zero pushable predicates) produces *correct rows with no pushdown* — invisible to any result-based assertion, and it falsifies the design doc's central claim. It runs in milliseconds with no infra, so the commit hook can carry it.
- [x] Convert the six gates into `test_` functions, keeping the hardcoded datasets
- [x] Keep the assertions as-is: predicate split (github 2 / jira 2), RLS counts 3/1/0, cross-source leaf stays residual, CLS digests with no `@`, ORDER BY total order
- [x] Delete `spike/` once green
- [x] Verify: `.venv/bin/python -m pytest -q tests/unit/test_ast_spike.py` → 6 passed

### Task T020: `README.md` — skeleton + Quickstart
**Files:** create `README.md`
**Decision:** stub **all** headings now and fill only Quickstart. A README written at the end describes what you meant to build, and it is brief deliverable #4 — a Phase 4 slip would otherwise lose a required deliverable outright.
- [x] Stub the six headings from the spec's README table, each marked with its owning phase
- [x] Fill **Quickstart**: `make up && make seed`, prerequisites, expected cold-start time
- [x] Add the one-line scope statement: this repo is deliverables **3 and 4**; the design doc and six-month plan are the Google Doc; `docs/design/design-doc.md` is a reference copy
- [x] State the test split in Quickstart: **`make test`** runs the unit suite and needs no Docker; **`make test-integration`** needs `make up` first — confirmed 2026-09-28, so a reviewer on a fresh clone is never staring at a container failure that looks like a broken test
- [x] Verify: a fresh reader can run the Quickstart top-to-bottom without asking a question

---

## Verification Gate

The phase is **not** done until all of these pass, regardless of checkbox state.

- [x] `make down && docker compose build && time make up` → `/healthz` returns `{"status":"ok"}` in **under 60 seconds** cold *(submission gate 3)*
- [x] `.venv/bin/python -m pytest -q tests/unit` → all green, **0 failures** *(this is also the commit hook)*
- [x] `make test-integration` → all green
- [x] `curl -s -X POST localhost:8000/v1/query -d '{"sql":"SELECT 1"}'` → **401** with a typed error body
- [x] `T=$(curl -s -X POST localhost:8000/v1/auth/mock-token -d '{"user":"alice","role":"support","tenant":"tenant_acme"}' | jq -r .token)` then `curl -s -X POST localhost:8000/v1/query -H "Authorization: Bearer $T" -d '{"sql":"SELECT 1"}' | jq -e '.trace_id != null'` → **200**, non-empty `trace_id`
- [x] `curl -s localhost:8000/metrics | grep -E 'rate_limit_remaining|http_request'` → **both** families present *(proves ADR-015's shared-registry assumption)*
- [x] `docker compose exec -T postgres psql -U postgres -c '\dt'` → 7 tables + `schema_migrations`
- [x] `git commit` succeeds — i.e. no file exceeds 500 lines and the unit hook passes
- [x] `src/models/` is frozen: `envelope.py`, `errors.py`, `context.py`, `request.py` match the spec's contracts field-for-field

**On green:** tick Phase 0's `build` and `gate` boxes in `docs/design/03-BUILD-PROCESS.md`, then `/verify` → `/code-review` → `/dev handoff`.

---

## Self-review

- [x] Every spec requirement traced to ≥1 task — T0 (done) · T1→T002 · T2→T003 · T3→T004 · T4→T006,T007 · T5→T008 · T6→T013,T014 · T7→T011 · T8→T012 · T9→T009,T010 · T10→T015,T016,T017 · T11→T020 · T12→T018,T019
- [x] Zero placeholder text
- [x] Every task has explicit files + a verification command
- [x] Foundational tasks (T005–T014) block the route tasks; no `[P]` markers — the human plan-review gate serializes the work anyway (`03-BUILD-PROCESS.md`)
- [x] Every foundational task carries its own test (LAW 6)
- [x] No task sized to produce a file over ~200 lines (LAW 1)
