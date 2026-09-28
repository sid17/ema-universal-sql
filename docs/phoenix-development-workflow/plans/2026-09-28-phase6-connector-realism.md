# Phase 6 — Connector Realism & the Swap Proof

> **Goal:** make the connector a *real* connector whose transport happens to read from memory,
> and ship a binary that shows its input and output. The claim being proved:
>
> > **Swap the transport for a real HTTP call and the system returns the same rows.**
>
> **Depends on:** Phase 1 (the connector contract, the six-step `fetch`, the token bucket) and
> Phase 2 (the planner, which decides what is pushed down).
>
> **Assumes:** the control plane is seeded from `config/connectors/*.yaml`. T601 changes that
> YAML, so **every task after T601 requires a reseed** (`make seed`).
>
> **Verify:** `make connectors` writes `docs/artifacts/connectors/connector-walkthrough.txt`;
> `pytest -q tests/unit` is green *including the swap proof*; `make demo` output is byte-identical
> to the committed artifact except for `trace_id` and timestamps.

---

## Why this phase exists

`RequestOption.inject_into` is **dead data**. Verified by grep:

| Site | What it does |
|---|---|
| `src/connectors/base.py:38` | defines `inject_into` |
| `src/planner/planner.py:221` | `PushedPredicate` carries `option=...` |
| `SourcePlan.fetch_predicates()` | flattens to `{column: (op, value)}` — **the option dies here** |
| `src/connectors/mock_adapter.py:259` | filters rows with a dict comprehension |
| everywhere else | tests asserting it round-trips from YAML |

Nothing reads `.inject_into` to decide where a value goes. The idea the design rests on — *a
capability is predicate support **plus** placement* — is declared, carried one hop, and discarded.
Today you could swap `path` and `query` in `github.yaml` and the whole suite still passes.

This phase makes the placement executable, which is what turns "the mock is a stand-in" into
"the mock is a connector with an in-memory transport".

---

## Design

### Three layers, one seam

```
SourcePlan ──► FetchRequest                       (unchanged — see D3)
                   │
                   ▼  build_request()             reads inject_into. SHARED, generic, real.
              OutboundRequest { method, url, path, query, headers, body }
                   │
                   ▼  _transport()                ◄── THE ONLY THING A LIVE ADAPTER REPLACES
              SourceResponse  { status, headers, body }
                   │
                   ▼  parse_response()            per connector — two real shapes, field mapping
              rows, next_cursor, has_more
```

**The load-bearing rule: the mock filters using the request it just built**, not using
`FetchRequest`. A wrong `inject_into` then produces wrong rows and a test fails.

### The two rate limits, which must not be conflated

| | What it is | Where | Status entering this phase |
|---|---|---|---|
| **Our governor** | per-tenant token bucket protecting the *downstream* quota | `TokenBucketRateLimiter`, Redis+Lua | built, tested, demoed |
| **The source's own limit** | what a real API *tells you* in response headers | nowhere | **missing** |

`consume()` already returns `RateLimitDecision(allowed, remaining, retry_after_ms)` and the adapter
**throws it away** (`await self._limiter.consume(...)`, no assignment). The source's rate-limit
headers are derived from that decision — so the two agree *by construction*, at zero extra cost
and with no second Redis call.

### Decisions taken

| # | Decision | Why |
|---|---|---|
| **D1** | Jira filters compile to **JQL** (`inject_into: jql`), GitHub's to path + query | Real Jira has no per-field query param. Two sources diverging on *every* axis — path vs query vs JQL, cursor vs offset, flat vs nested — absorbed by one contract, is the whole claim. |
| **D2** | Response bodies carry **real nesting**, and `parse_response` does **field mapping** | Real GitHub returns `user.login`, not `author`. Flat dicts that already match our column names would leave the parse step with nothing to do, and "swap for the real API" would be false. |
| **D3** | `FetchRequest` is **unchanged**; `build_request` re-derives placement from the capability model | Threading `RequestOption` through the planner→adapter boundary would put placement in two places. Re-deriving keeps the adapter the single site where placement is executed. |
| **D4** | The credential is **real, redacted at the render boundary** | `SecretsManagerClient.resolve` already returns the tenant's decrypted token at step 3. It flows into `Authorization` exactly as it would live; only the renderer redacts. Proves per-tenant credential isolation instead of asserting it. |
| **D5** | `HttpTransport` lives in **tests**, not `src/` | `httpx` is a dev dependency and `03-BUILD-PROCESS.md` forbids later phases editing `pyproject.toml`. Consequence, and it is an improvement: the swap proof needs no container and **runs on the commit hook**. `src/` defines the seam; the test is the worked example a live adapter copies. |
| **D6** | The lab stays **below entitlement** | It demonstrates the connector contract. RLS/CLS are Phase 2's story and `make demo` already tells it. |
| **D7** | `mock_adapter.py` is decomposed **before** anything is added to it | It is at 413 lines, already past LAW 1's 400-line decompose threshold. |

---

## File map

| File | Action | Task |
|---|---|---|
| `src/connectors/mock_adapter.py` | modify — shrink, then re-route `fetch` | T600, T609, T610 |
| `src/connectors/validate.py` | create — step 0, extracted | T600 |
| `src/connectors/base.py` | modify — `InjectInto += "jql"`, `PaginationSpec.size_option` | T601 |
| `config/connectors/github.yaml` | modify — `size_option: per_page` | T601 |
| `config/connectors/jira.yaml` | modify — four columns to `jql`, `size_option: maxResults` | T601 |
| `tests/unit/catalog_fixture.py` | modify — mirror the YAML | T601 |
| `src/connectors/request.py` | create — `OutboundRequest`, `build_request`, redaction | T602, T604 |
| `src/connectors/github.py` | modify — endpoint, auth, render, parse | T603, T607, T608 |
| `src/connectors/jira.py` | modify — endpoint, auth, render, parse | T603, T607, T608 |
| `src/connectors/response.py` | create — `SourceResponse`, rate-limit headers | T605, T606 |
| `src/connectorlab/__main__.py` | create — CLI entry | T613, T615 |
| `src/connectorlab/scenes.py` | create — the five scenes | T614 |
| `src/connectorlab/render.py` | create — the printer | T613 |
| `tests/unit/test_request_build.py` | create | T602, T604 |
| `tests/unit/test_source_response.py` | create | T605, T606 |
| `tests/unit/test_connector_roundtrip.py` | create | T607, T608 |
| `tests/unit/test_transport_swap.py` | create — **the swap proof** | T611, T612 |
| `tests/unit/test_connectorlab.py` | create | T614, T615 |
| `tests/integration/test_rate_limit_agreement.py` | create | T617 |
| `Makefile` | modify — `connectors` target | T616 |
| `docs/artifacts/connectors/connector-walkthrough.txt` | create (generated) | T616 |
| `README.md` | modify — one section | T618 |

---

## Sequencing rule

**The YAML change (T601) lands before any code reads it, and the decomposition (T600) lands before
any code is added.** Everything in Milestone B depends on Milestone A being reseeded — a task run
against a stale control plane will pass for the wrong reason, which is the failure mode that cost a
whole load-test run in Phase 5b.

---

## Milestone A — the request layer

- [x] **T600 — decompose `mock_adapter.py` before adding to it (LAW 1)**
  Extract step 0 (`_validate`, `_as_op_value`) to `src/connectors/validate.py`. Pure move, no
  behaviour change.
  **Decision:** extract *validation* rather than the six-step orchestration, because the ordering
  comment on `fetch()` is the file's most valuable documentation and splitting it would strand the
  comment from the code it describes.
  **Files:** `src/connectors/validate.py` (create), `src/connectors/mock_adapter.py`
  **Verify:** `.venv/bin/python -m pytest -q tests/unit` green; `wc -l src/connectors/*.py` shows
  no file over 400.

- [x] **T601 — `jql` placement and `size_option`**
  Add `"jql"` to `InjectInto`. Add `size_option: RequestOption` to `PaginationSpec` (`per_page` for
  GitHub, `maxResults` for Jira — real APIs name the page-size param differently, and hard-coding
  it per connector would put data in code). Move Jira's four `key_columns` to
  `inject_into: jql`. Mirror both into `tests/unit/catalog_fixture.py`.
  **Watch-out:** `supports()` reads only `ops` and `_require_key_columns` reads only `require`, so
  the planner is unaffected — assert that rather than assuming it.
  **Files:** `src/connectors/base.py`, `config/connectors/*.yaml`, `tests/unit/catalog_fixture.py`
  **Verify:** `make seed` then `.venv/bin/python -m pytest -q tests/unit tests/integration/test_seed.py`

- [x] **T602 — `build_request()`**
  `src/connectors/request.py`: frozen `OutboundRequest(method, host, path, query, headers, body)`
  and `build_request(adapter_spec, capabilities, request, credential) -> OutboundRequest`, driven
  **entirely** by the capability model — `path` substitutes into the endpoint template, `query`
  becomes a param, `header` a header, `jql` composes into one expression, plus the pagination
  token and page size from `token_option` / `size_option`.
  **Decision:** JQL renders `status = "In Progress" AND assignee = "alice"` with operators taken
  from the predicate, so Jira's range ops on `updated` render as `updated >= "…"` — which is the
  only place in the system where a non-equality op is visibly *used* rather than declared.
  **Files:** `src/connectors/request.py`, `tests/unit/test_request_build.py`
  **Verify:** tests cover all four placements, both pagination strategies, and that swapping
  `path`↔`query` in a capability dict changes the built URL.

- [x] **T603 — endpoint and auth descriptors on the adapters**
  Class-level, next to `resource`: `host`, `endpoint` (template), `auth_scheme`
  (`bearer` | `basic`), `api_headers` (GitHub's `Accept: application/vnd.github+json` and
  `X-GitHub-Api-Version: 2022-11-28`; Jira's `Accept: application/json`).
  **Decision:** on the class, not in the YAML. `resource` is already there for the same reason —
  it names what the class *is*, where `capabilities` describes what the upstream API *accepts*.
  **Files:** `src/connectors/github.py`, `src/connectors/jira.py`
  **Verify:** `pytest -q tests/unit/test_request_build.py` renders
  `GET /repos/ema/core/pulls?state=open&per_page=5` and
  `GET /rest/api/3/search?jql=…&startAt=0&maxResults=5`.

- [x] **T604 — redaction at the render boundary**
  `OutboundRequest.redacted()` returns a copy with `Authorization` replaced by
  `Bearer ghp_****` / `Basic ****`. The real value stays on the object.
  **Files:** `src/connectors/request.py`, `tests/unit/test_request_build.py`
  **Verify:** a test asserts the resolved plaintext token appears in `request.headers` and
  **does not** appear anywhere in `render(request)` output.

## Milestone A2 — one connector, many API calls  *(added mid-build)*

Raised while T605 was in flight: an adapter took exactly one endpoint, so a
second GitHub API call would have meant a second adapter **class**. That
contradicts the claim the brief grades — admins onboard "via console or config"
(line 29) — and it would have baked the wrong shape in, because T603 had just
put `endpoint` on the class as a constant.

**What was actually wrong.** `connectors` was `PRIMARY KEY (connector_type)` and
the YAML's `resource:` was *never persisted at all* — the seeder validated
`connector_type`, `version`, `capabilities` and dropped the rest. `resource`
existed only as a Python class attribute. Meanwhile `policies` was **already**
keyed `(tenant_id, connector_type, resource)`: the data model had anticipated
multiple resources per connector, and only this one table had not.

| # | Decision | Why |
|---|---|---|
| **D8** | One YAML file per **connector**, with an `api:` block and a `resources:` map | The `api:` half (host, auth, headers, rate-limit dialect) is true of every call; the `endpoint:` half is true of one. Splitting them is what stops a second GitHub file from duplicating — and drifting from — the first. |
| **D9** | The seeder writes **one row per resource**, composing `api` + `endpoint` | The YAML is the authoring source; the row is the read model an adapter wants. `compose_endpoint()` is shared by the seeder and the test fixtures, so a divergence cannot hide until integration. |
| **D10** | Drop the `tenant_connector → connectors` FK rather than widening it | A **grant is per connector, not per endpoint**. One GitHub grant and one GitHub budget cover every GitHub resource because they protect one upstream quota. A composite FK would have forced a grant per endpoint and silently multiplied every tenant's budget. |
| **D11** | Migration 003 **recreates** `connectors` instead of backfilling | There is no endpoint data to migrate from, and inventing a host would put a fabricated URL in a reviewer's database. The catalog is rebuilt by `make seed` on every run, and the registry already tolerates an empty one by warning. |
| **D12** | `ADAPTER_CLASSES` tuple → `ADAPTERS` dict keyed by `connector_type` | The class is selected by *kind*; everything else about a source is a row. The registry now enumerates `list_connectors()`. |

- [x] **T619 — migration 003: `connectors` keyed by `(connector_type, resource)`**
  Adds `resource`, `endpoint JSONB`, `rate_limit JSONB`; drops the FK.
  **Files:** `migrations/003_connector_resources.sql`
  **Verify:** `docker compose logs app | grep migration` → "3 migration(s) on record" ✅

- [x] **T620 — the YAML gains `api:` + `resources:`**
  **Files:** `config/connectors/{github,jira}.yaml`

- [x] **T621 — the seeder writes one row per resource**
  Validates `api.host` / `api.auth_scheme` / `api.rate_limit` and each resource's
  `endpoint.path` + `capabilities`; refuses a connector declaring no resources.
  **Files:** `scripts/seed.py`, `src/connectors/request.py` (`compose_endpoint`)
  **Verify:** `make seed` → `connectors 2 rows`; `psql` shows both endpoints ✅

- [x] **T622 — `EndpointSpec.from_dict` / `RateLimitDialect.from_dict`**
  The two hard-coded module constants in `response.py` are deleted: the dialect
  is now seeded data, so a constant would be a second source of truth.
  **Files:** `src/connectors/{request,response}.py`

- [x] **T623 — the registry becomes row-driven**
  `catalog()` and `adapters()` enumerate rows; `adapters()` is keyed
  `(connector_type, resource)`; `resource`/`endpoint`/`rate_limit` become
  instance state; `dataset()` is looked up in `DATASETS` rather than overridden.
  **Files:** `src/pipeline/registry.py`, `src/connectors/{mock_adapter,github,jira}.py`,
  `src/control_plane/repository.py` (`list_connectors`, `get_connector`),
  `src/execution/federation.py` (`_adapter_for` keys on the pair)

- [x] **T624 — fixtures mirror the new authoring shape**
  `catalog_fixture` carries the `api:` blocks and composes them through the same
  `compose_endpoint` the seeder uses.
  **Files:** `tests/unit/{catalog_fixture,conftest}.py` + 8 call sites

- [x] **T625 — the proof: a third resource, no new class**
  Seeds `github.issues` at runtime; asserts one class serves both, each renders
  its own path, the new one fetches its own rows, and the two failure modes are
  loud (no dataset → refused; unknown kind → skipped with a warning).
  **Files:** `tests/unit/test_connector_onboarding.py` (7 tests)
  **Verify:** `pytest -q tests/unit` → 643 passed ✅; `pytest tests/integration` → 103 ✅

**Honest limit of the claim.** A *mock* resource still needs its rows in
`DATASETS` — mock data has to come from somewhere. A **live** adapter fetches
instead, so for a live connector a new API call is configuration and nothing
else. `test_connector_onboarding.py`'s docstring says exactly this rather than
overstating it.

---

## Milestone B — the response layer

- [x] **T605 — `SourceResponse`**
  `src/connectors/response.py`: frozen `SourceResponse(status, headers, body)`, where `body` is the
  parsed JSON a real client would receive.
  **Files:** `src/connectors/response.py`, `tests/unit/test_source_response.py`
  **Verify:** `pytest -q tests/unit/test_source_response.py`

- [x] **T606 — rate-limit headers, from the decision we already have**
  Capture the `RateLimitDecision` that `consume()` returns (currently discarded) and render:
  GitHub `X-RateLimit-Limit` (= `policy.capacity`) / `-Remaining` (= `decision.remaining`) /
  `-Reset` (from `policy.refill_ms`) / `-Resource: core`; Jira the same three plus `Retry-After`.
  Exhaustion shapes diverge the way they really do — **GitHub `403` with `X-RateLimit-Remaining: 0`**
  (its primary limit), **Jira `429` + `Retry-After`** — and both normalise to one
  `ApiError(RATE_LIMIT_EXHAUSTED)`.
  **Decision:** derived from the decision object, never from a second `remaining()` call. A second
  call would be a second Redis round trip *and* could disagree with what was actually spent.
  **Files:** `src/connectors/response.py`, `src/connectors/mock_adapter.py`, `tests/unit/test_source_response.py`
  **Verify:** four tests — (1) headers equal the bucket's post-consume state; (2) **a cache hit
  emits no rate-limit headers and spends no token**; (3) exhaustion raises *before* `_transport` is
  reached, asserted with a transport spy; (4) `Retry-After` ≥ `policy.refill_ms`.

- [x] **T607 — render the page as the source would (the mock transport)**
  Per connector: GitHub → a bare JSON **array** of PR objects (`user: {login}`, `head: {repo: {full_name}}`)
  plus a `Link: <…>; rel="next"` header and `ETag`; Jira → `{"startAt":…,"maxResults":…,"total":…,"issues":[{"key":…,"fields":{…}}]}`.
  **Files:** `src/connectors/github.py`, `src/connectors/jira.py`, `tests/unit/test_connector_roundtrip.py`
  **Verify:** rendered payloads match committed shape fixtures.

- [x] **T608 — `parse_response()` and field mapping**
  Map back: `user.login → author`, `head.repo.full_name → repo`, `fields.status.name → status`,
  `fields.assignee.name → assignee`, `fields.reporter.emailAddress → reporter_email`. Cursor from
  the `Link` header (GitHub) vs `startAt + total` (Jira).
  **Watch-out:** this is the riskiest task in the phase — a round-trip bug silently changes rows.
  **Files:** `src/connectors/github.py`, `src/connectors/jira.py`, `tests/unit/test_connector_roundtrip.py`
  **Verify:** property test — `parse_response(render_page(rows)) == rows` for every row in both
  fixtures, and for the empty page.

- [x] **T609 — route `fetch()` through build → transport → parse**
  Steps 4+5 become: `build_request` → `_transport` → `parse_response`. `_transport` filters
  **using the built request's query/JQL/path**, not using `FetchRequest`.
  **Files:** `src/connectors/mock_adapter.py`, `tests/unit/test_mock_adapter.py`
  **Verify:** the full suite green, **and** a test that corrupts `inject_into` in a capability dict
  and asserts the returned rows change — the assertion that is impossible today.

- [x] **T610 — the conditional request**
  The revalidation branch sends `If-None-Match` and the transport answers **`304` with an empty
  body**, instead of computing a candidate page and comparing ETags locally.
  **Decision:** this is simpler *and* more real. The existing guarantee — a 304 spends no token —
  is preserved and must be re-asserted, not assumed.
  **Files:** `src/connectors/mock_adapter.py`, `tests/unit/test_mock_adapter.py`
  **Verify:** the existing staleness tests pass unchanged; a new test asserts the `304` path emits
  no rate-limit headers.

## Milestone C — the swap proof

- [x] **T611 — extract the `_transport` seam**
  `async def _transport(self, outbound: OutboundRequest) -> SourceResponse` on
  `MockConnectorAdapter`, with a docstring naming `tests/unit/test_transport_swap.py` as the
  worked example of a live implementation.
  **Files:** `src/connectors/mock_adapter.py`
  **Verify:** `pytest -q tests/unit`

- [x] **T612 — the swap proof itself**
  A Starlette app replaying the rendered payloads, reached over **real HTTP** through
  `httpx.ASGITransport`; an adapter subclass whose `_transport` is that httpx call. Assert the
  **same rows, cursor and `has_more`** come out through both transports for: page 1, a middle page,
  the last page, an empty result, and the `304`.
  **Decision:** `tests/unit`, not `tests/integration` — it needs no Docker, so the swap proof runs
  on the commit hook (D5).
  **Files:** `tests/unit/test_transport_swap.py`
  **Verify:** `.venv/bin/python -m pytest -q tests/unit/test_transport_swap.py`

## Milestone D — the binary

- [x] **T613 — `src/connectorlab/` skeleton and the printer**
  `render.py` formats an `OutboundRequest` as a wire-shaped request block and a `SourceResponse` as
  a status line + headers + pretty body. **The lab prints; it never simulates** — every value it
  shows comes from the real fetch path.
  **Files:** `src/connectorlab/{__init__,__main__,render}.py`
  **Verify:** `docker compose exec -T app python -m src.connectorlab --help`

- [x] **T614 — the five scenes**
  `capabilities` (the matrix, read from the seeded control plane) · `request` (FetchRequest → the
  call we would send) · `fetch` (full `AdapterResponse`, run twice so `served` flips `live`→`cache`) ·
  `paginate` (`limit=5` over 20 rows → 4 pages, tokens decoded, `has_more` flipping, cross-strategy
  token rejected) · `ratelimit` (headers ticking down, then the two exhaustion shapes).
  **Files:** `src/connectorlab/scenes.py`, `tests/unit/test_connectorlab.py`
  **Verify:** each scene has a test asserting its output contains the value the real path produced.

- [x] **T615 — ad-hoc query mode**
  `python -m src.connectorlab fetch github --where repo=ema/core --where state=open --limit 5`,
  `--json` for machine-readable output.
  **Files:** `src/connectorlab/__main__.py`, `tests/unit/test_connectorlab.py`
  **Verify:** an unsupported predicate exits non-zero with the capability model's own message.

- [x] **T616 — `make connectors` and the artifact**
  Runs in-container like `make seed`; tees to `docs/artifacts/connectors/connector-walkthrough.txt`.
  **Files:** `Makefile`, `docs/artifacts/connectors/`, `docs/artifacts/README.md`
  **Verify:** `make connectors` writes a non-empty artifact containing **no plaintext credential**.

## Milestone E — wire it to the system

- [x] **T617 — the source and the system agree**
  Integration test: drain a tenant's GitHub budget through `POST /v1/query`, assert the envelope's
  `rate_limit_status` equals what the source headers reported on the last successful fetch, and that
  the 429 carries `Retry-After`.
  **Files:** `tests/integration/test_rate_limit_agreement.py`
  **Verify:** `make test-integration`

- [x] **T618 — document it**
  One README section: the three layers, the swap proof, and `make connectors`.
  **Files:** `README.md`, `docs/artifacts/README.md`
  **Verify:** the README's commands run as written on a fresh clone.

---

## What changed against the plan

Five things the plan did not anticipate. All are recorded because a plan that
matches the build only after the build is rewritten is not a record of anything.

| # | What | Why |
|---|---|---|
| 1 | **`src/connectors/inbound.py` added** — reads predicates and the page window back *off the built request* | Not in the plan, and the phase's whole claim depends on it. Without it the transport would filter on `FetchRequest.predicates`, agree with the caller by construction, and `inject_into` would still be decoration. Going through the wire form means a placement bug returns **wrong rows**. It also means the JQL this build renders must be JQL it can parse. |
| 2 | **`MockTransport` extracted** to its own module | `mock_adapter.py` hit 457 lines. The cut is a real seam, not a convenient one: `mock_adapter` owns the *order* a fetch happens in (governance), `mock_transport` owns *what a call looks like* (wire) — and the wire half is what a live adapter replaces. `HTTP_STATUS_FOR_CODE` moved to `errors.py` to keep the two from importing each other. |
| 3 | **`header_value()` — case-insensitive header lookup** | Found by T612, which is what it is for. ASGI and httpx hand headers back lowercased, so a live transport reads `link` and `if-none-match` where the mock wrote `Link` and `If-None-Match`. A plain `dict.get` returns `None`: pagination silently stops after one page and the conditional request never matches, with every in-memory test still green. |
| 4 | **The 304 path was dead code that passed its tests** | T610 exposed it. The cached ETag was one we computed over *parsed rows*, while the transport issues one over the *API body* — so `If-None-Match` could never match anything. The cached validator is now the source's, read off the response. The existing staleness tests pass unchanged, which is the point: they could not have caught this. |
| 5 | **`scripts/seed.py` and `tests/unit/conftest.py` decomposed** | Both crossed LAW 1's 400-line threshold from this phase's additions. `scripts/seed_connectors.py` (the one *global* catalog, where every other section writes per-tenant rows) and `tests/unit/fakes.py` (the classes, where `conftest` keeps the fixtures). |

**The walkthrough queries `tenant_load` and drains `tenant_globex`, never
`tenant_acme`.** The first version used acme and emptied its 5+2 GitHub budget —
the budget `make demo` needs intact for its 429 to be deterministic. An artifact
generator that quietly breaks another artifact is worse than one that prints less.

**Named, not fixed:** `ConnectorBudget` publishes `remaining` and `throttled` but
not the ceiling, so an envelope reader sees "4 left" without "of how many" — while
the source's own `X-RateLimit-Limit` header does carry it. Adding `limit` would
change a published response contract (HLD §4), so it is recorded in
`tests/integration/test_rate_limit_agreement.py` rather than changed here.

---

## Two defects found during verification — both pre-existing, both fixed

Neither is Phase 6's doing. Both come from Phase 5's `WORKERS=8` default, and
both surfaced only when the volume was torn down and the whole cold path run —
which is the path a reviewer takes and the one nothing had exercised.

### D1 — seven workers crashed on every fresh start

All eight workers call `run_migrations()` in their lifespan. Postgres's
`CREATE TABLE IF NOT EXISTS` is **not** race-safe: concurrent creates both pass
the existence check and the losers get
`UniqueViolation on pg_type_typname_nsp_index`. Seven of eight died with
"Application startup failed. Exiting."; uvicorn respawned them and the retry
succeeded, so the stack came up healthy in 4s **while dumping seven tracebacks
into the log of every fresh clone.**

Fixed with a session-level `pg_advisory_lock` around the whole run, ledger read
included — that read is the statement that creates `schema_migrations`, so it is
the one that raced. Held on one connection for the duration, because a session
lock belongs to the session that took it.

`tests/integration/test_migration_race.py` runs eight concurrent startups, each
with its own pool. **Verified to fail without the lock** (`RuntimeError:
migration 001_init.sql failed`) before being accepted as a test.

| | before | after |
|---|---|---|
| startup failures, cold volume | **7** | **0** |
| healthy after | 4s | 2s |

### D2 — `make demo` step 4 worked about one time in eight

`fail_next` kept the armed failure in a dict on `ConnectorRegistry`, which is
per *process*. `scripts/demo.sh` uses a separate `curl` per call, so each call
opens a new TCP connection and lands on whichever worker accepts it: the arm
went to one worker, the query to another. **The committed artifact said
`partial: true`; a fresh run said `partial: false`.**

This is MUST-tier — DoD §2 hard part 5, brief line 84 — and the demo is the
artifact that proves it.

The integration suite never caught it because `httpx` keeps one connection alive
and therefore sticks to one worker: `test_timeout_partial.py` passed 6/6 under
the same eight workers that broke the demo. *A bug only the artifact can see is
the worst kind to leave in.*

Fixed by moving the hook into Redis (`src/governance/failure_hooks.py`), read
with `GETDEL` so "one-shot" holds across workers too. **Wired only when
`TEST_MODE` is set**, so outside test mode the hook is not merely refused by the
route — it is absent from the fetch path and costs nothing per request.
`ConnectorRegistry.adapters()` became `async` for this, and awaits nothing when
no store is wired.

`tests/unit/test_failure_hooks.py` (13 tests). The regression test builds **two
registries over one Redis** — which is what two workers are — arms through the
first and asserts the second fires. The old design could not pass it.

Verified: three consecutive `./scripts/demo.sh` runs under `WORKERS=8`, all
reporting `partial: true`, `join_status: incomplete`, `SOURCE_TIMEOUT`.

---

## Verification Gate

The phase is not done until every line below passes, regardless of checkbox state.

```bash
.venv/bin/python -m pytest -q tests/unit          # includes the swap proof (T612)
make up && make seed
make test-integration
make connectors                                   # writes the artifact
make demo                                         # unchanged but for trace_id/timestamps
.venv/bin/ruff check src tests scripts
wc -l src/connectors/*.py src/connectorlab/*.py   # nothing over 400
grep -r "ghp_\|Basic [A-Za-z0-9+/=]\{8,\}" docs/artifacts/connectors/   # must find nothing real
```

**Expected:** unit green including `test_transport_swap.py`; the artifact shows a GitHub call with
`repo` in the path and a Jira call with a JQL expression; rate-limit headers tick down and both
exhaustion shapes appear; no credential in the artifact.

### Measured, 2026-09-28

| Check | Result |
|---|---|
| `pytest tests/unit` | **683 passed** (was 617 entering the phase) |
| `pytest tests/integration` | **113 passed** (was 100) |
| cold start, empty volume | **2s**, zero startup failures (was 4s and seven) |
| `./scripts/demo.sh` under `WORKERS=8` | `partial: true` on three consecutive runs |
| `ruff check src tests scripts` | clean |
| files over 400 lines | `tests/unit/test_entitlement.py` (401) — pre-existing, carried in `LOAD-NEXT-STEPS.md` |
| `make connectors` | 234-line artifact |
| credentials in `docs/artifacts/` | none |
| `make demo` | diff is stage timings only — no behavioural change |

---

## Watch-outs

1. **Reseed after T601.** Capabilities live in Postgres. A task run against a stale control plane
   passes for the wrong reason — the exact failure that invalidated a load run in Phase 5b.
2. **T608 is the risky one.** Field mapping is the only place a bug silently changes *rows* rather
   than failing loudly. The round-trip property test is the guard, not a nicety.
3. **`tests/unit` must stay infra-free** — the commit hook runs it. The replay server is ASGI
   in-process; no port, no container.
4. **LAW 7.** If `ruff` complains about the JQL builder's complexity, split the function. Do not
   add an ignore.
5. **The lab prints, it never simulates.** The moment it constructs a URL the adapter did not, it
   is a brochure — and a reviewer cannot tell from the outside, which is why it has to be true.
