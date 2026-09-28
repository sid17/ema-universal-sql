# Plan — Phase 1: Connectors + Governance

**Goal:** two deterministic mock connectors behind one contract, plus the three governance primitives that
make them multi-tenant-safe (token bucket, freshness cache, per-tenant secrets), seeded from YAML. No SQL.

**Depends on:** Phase 0 (shipped, `7d5ab97`) — `src/models/`, `ControlPlaneRepository`, `ApiError`, the
`stage_span` decorator, the `rate_limit_remaining` gauge, and the four seeded tenant rows.

**Assumes:**
1. `src/models/` is **frozen**. Phase 1 imports `ApiError`, `ErrorCode`, `UserContext`; it does not edit them.
2. Tenants are **already seeded** by `002_seed_tenants.sql`. Phase 1 must not re-seed or renumber them.
3. `rate_limit_policies` is keyed `(tenant_id, connector_type)` with no per-user row — the basis of ADR-020.
4. Postgres and Redis publish **no host ports**, so anything touching them runs inside the container.
5. Phase 2 will consume `AdapterResponse` and `failure_type` unchanged. If either shape is wrong, Phase 2's
   partial-result gate is where it surfaces.

**Verify (the phase gate — all must pass):**
```bash
make test                      # all 7 acceptance tests green, inside the hermetic suite
make up && make seed           # seeding is real, and re-running it is idempotent
make test-integration          # seed round-trips through the control-plane reads
.venv/bin/ruff check src tests scripts   # clean, no ignore list (LAW 7)
```

---

## Step 0 — inspect real data (DONE, before planning)

- [x] **T000** Probe `fakeredis[lua]` against a draft of the real token-bucket Lua via `register_script()`.
  **Result:** `capacity=7 (5+2 burst)`, `refill=12000ms` → 7 admitted back-to-back, 8th `allowed=0
  retry_after_ms=12000`; after exactly one refill interval exactly one more admitted. Returns a `list`.
  **Decision:** unit tests run the **real Lua**, not a Python reimplementation, because two implementations of
  the same arithmetic is how a unit test passes while production is wrong — the exact failure Phase 0's AST
  spike caught. Installed: `fakeredis==2.38.0`, `lupa==2.8`, `sortedcontainers==2.4.0`.

## File map

| File | Action | Task |
|---|---|---|
| `pyproject.toml` | modify — declare `pyyaml`; add `fakeredis[lua]` to `dev` | T001 |
| `src/config.py` · `.env.example` · `docker-compose.yml` · `tests/unit/test_config.py` | modify — `CACHE_TTL_MS` 60000 → 300000 | T001 |
| `src/governance/__init__.py` · `clock.py` | create | T002 |
| `src/connectors/__init__.py` · `base.py` | create | T003 |
| `src/connectors/errors.py` | create | T004 |
| `src/governance/ratelimit.py` · `tests/unit/test_ratelimit.py` | create | T005 |
| `src/governance/cache.py` · `tests/unit/test_cache.py` | create | T006 |
| `src/governance/secrets.py` · `tests/unit/test_secrets.py` | create | T007 |
| `src/connectors/mock_data.py` · `tests/unit/test_mock_data.py` | create | T008 |
| `src/connectors/pagination.py` · `tests/unit/test_pagination.py` | create | T009 |
| `src/connectors/github.py` · `jira.py` · `tests/unit/test_connectors.py` | create | T010 |
| `tests/unit/test_fetch_order.py` | create | T011 |
| `config/connectors/github.yaml` · `jira.yaml` · `grants.yaml` · `policies.yaml` · `rate_limits.yaml` | create | T012 |
| `scripts/seed.py` · `tests/integration/test_seed.py` | create | T013 |
| `Dockerfile` · `Makefile` | modify — copy `config/`+`scripts/`; real `seed` target | T014 |
| `README.md` | modify — Quickstart becomes true; status; deferred-machinery note | T015 |
| `tests/unit/conftest.py` | modify/create — `FakeClock` + `fake_redis` fixtures | T002, T005 |

**Not created:** no `003_seed.sql` (ADR-021). **Not touched:** `src/models/`, `src/gateway/`, `migrations/`.

---

## Foundational (blocking — nothing else starts until these are green)

- [x] **T001 — Dependencies and the `CACHE_TTL_MS` change, as one atomic edit**
  Files: `pyproject.toml`, `src/config.py`, `.env.example`, `docker-compose.yml`, `tests/unit/test_config.py`
  **Decision:** `pyyaml` is declared explicitly rather than relied on transitively through `uvicorn[standard]`,
  because config loading is a first-class deliverable (brief line 29) and a transitive dep can vanish in a
  minor release. `fakeredis[lua]` goes under `dev` only — it must never reach the image.
  **Decision:** `CACHE_TTL_MS` 60000 → **300000** (ADR-023). All four files move together or the build goes
  red. `tests/unit/test_config.py:43` asserts the old default; it is **restated to 300000, not loosened** —
  if this change ever requires weakening that assertion instead, the change is wrong (LAW 7).
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_config.py` → green; `grep -rn 60000 src/ .env.example docker-compose.yml` → no `CACHE_TTL` hit.

- [x] **T002 — `src/governance/clock.py` + fixtures**
  Files: `src/governance/__init__.py`, `src/governance/clock.py`, `tests/unit/conftest.py`
  `NowMs = Callable[[], int]`; `wall_clock_ms()` default; `FakeClock(start_ms)` with `.advance(ms)`.
  **Decision:** wall-clock epoch ms, **not** monotonic — bucket state lives in Redis and is compared across
  processes, where monotonic is meaningless (ADR-019, citing `PyrateLimiter`'s `AbstractClock`).
  **Decision:** `FakeClock` ships in `src/`, not `tests/`, because `scripts/seed.py` and future phases take a
  clock too; the *fixture* that wires it lives in `conftest.py` so the ergonomic path in tests is the correct one.
  Tests (same task): `FakeClock` advances deterministically; the default is wall-clock and within 2s of `time.time()`.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_clock.py`

- [x] **T003 — `src/connectors/base.py`: the contract**
  Files: `src/connectors/__init__.py`, `src/connectors/base.py`
  `RequestOption`, `CapabilityModel`, `AdapterResponse`, `BaseConnectorAdapter(ABC)` — transcribed from the
  phase file's typed block, which is locked (ADR-007: `fetch()` is the single live-adapter seam).
  **Decision:** plain `@dataclass`, not Pydantic. These are internal contracts that never cross an HTTP
  boundary; only `src/models/envelope.py` is serialized. Keeps validation cost off the per-fetch path.
  **Decision:** `capabilities()` returns a `CapabilityModel` built **from the seeded dict**, so the adapter
  and the control plane cannot disagree about what is filterable. Phase 2 reads the same dict.
  Tests (same task): `CapabilityModel.from_dict()` round-trips a seeded capability JSON; `BaseConnectorAdapter`
  cannot be instantiated; a subclass missing `fetch` raises `TypeError`.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_connector_base.py`

- [x] **T004 — `src/connectors/errors.py`: the vocabulary (not the machinery)**
  Files: `src/connectors/errors.py`
  `Action` (`SUCCESS|RETRY|RATE_LIMITED|REFRESH_TOKEN_THEN_RETRY|FAIL|IGNORE`), `FailureType`
  (`transient_error|config_error|system_error`), and `classify()` mapping a mock failure to
  `(Action, FailureType, ErrorCode)`.
  **Decision:** build the vocabulary, **not** the circuit breaker / backoff / retry loop (ADR-022). Phase 2
  must tell a timeout (→ `partial`) from an auth error (→ fail) from a throttle (→ `429`), and that
  three-way split *is* `failure_type` — a consumer one phase away, so not speculative. A status→action table
  would map HTTP statuses a mock never produces (LAW 5).
  Tests (same task): every `ErrorCode` the adapters can raise maps to exactly one `FailureType`; a timeout
  classifies `transient_error`, an auth failure `config_error`.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_connector_errors.py`

## Governance primitives — independent, [P] after T002

- [x] **T005 [P] — `TokenBucketRateLimiter`** · gate: `test_bucket_drain`, `test_bucket_burst`
  Files: `src/governance/ratelimit.py`, `tests/unit/test_ratelimit.py`, `tests/unit/conftest.py` (fake redis fixture)
  One Lua script (refill + consume, atomic) returning `[allowed, remaining, retry_after_ms]`; key
  `ratelimit:{tenant_id}:{connector_type}`; capacity `max_requests + burst` from the policy row; `consume()`
  raises `ApiError(RATE_LIMIT_EXHAUSTED, 429, retry_after_ms=…)` on empty; `remaining(tenant, connector)`
  for the envelope and the gauge.
  **Decision:** one composite key, not three nested buckets (ADR-020) — `rate_limit_policies` has no per-user
  row, so a third bucket could only be given an invented limit.
  **Decision:** `remaining` comes back from the **same** script call as the consume, not a follow-up read —
  a second call is not atomic with the first, so the gauge could report a figure that was never true.
  Tests (same task, using T000's verified numbers): cold bucket admits `max_requests + burst` back-to-back;
  the next raises with `retry_after_ms > 0`; `remaining()` reads 0; after one refill interval **exactly one**
  more is admitted. All via `FakeClock` + `fakeredis` running the real Lua.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_ratelimit.py -v`

- [x] **T006 [P] — `FreshnessCacheManager`** · gate: `test_cache_hit`, `test_cross_tenant_isolation`
  Files: `src/governance/cache.py`, `tests/unit/test_cache.py`
  Key `cache:{tenant_id}:{entitlement_scope}:{connector}:{sha1(normalized_request)}`; value
  `{fetched_at, etag, data}`; fixed server TTL from `CACHE_TTL_MS`; `get(key, max_staleness_ms)` enforcing
  staleness **on read**; the `304` path refreshing `fetched_at` without a token.
  **Decision:** `entitlement_scope` is a **required positional argument** from day one (ADR-025), even though
  Phase 2 is what computes a real one — Phase 1 passes the caller's role as a documented placeholder. A
  missing scope segment does not degrade the cache, it serves one user's rows to another.
  **Decision:** TTL is a property of the **write**, `max_staleness_ms` of the **read**. A
  `max_staleness_ms=0` request must not write a zero-TTL entry, or no later request could ever hit.
  **Decision:** no single-flight guard (DoD COULD, #13) — revisit at Phase 4 only if k6 shows a real herd.
  Tests (same task): hit within TTL → `served="cache"`; stale-but-present + matching etag → `304` refreshes
  `fetched_at`; `tenant_globex`'s entry for a byte-identical request is **not** read by `tenant_acme`; and
  the key string literally contains both the tenant and the scope — asserted on the key, not only on the miss,
  because a miss-only test passes against a scheme that leaks for a different pair of users.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_cache.py -v`

- [x] **T007 [P] — `SecretsManagerClient`** · gate: `test_secret_indirection`, `test_crypto_shred`
  Files: `src/governance/secrets.py`, `tests/unit/test_secrets.py`
  `resolve(secret_ref) -> token`: look up `secrets.ciphertext`, Fernet-decrypt with **that tenant's**
  `tenants.fernet_key`. Never returns another tenant's secret.
  **Decision:** the tenant is derived from the `secrets` row, not passed by the caller — a caller-supplied
  tenant is a confused-deputy seam, and this class exists to prove isolation.
  Tests (same task): acme and globex refs decrypt to **different** tokens; a globex ref cannot be decrypted
  with acme's key; deleting globex's `fernet_key` makes its ciphertext permanently unrecoverable while
  acme's still decrypts (crypto-shred, HLD §2).
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_secrets.py -v`

## Connectors

- [x] **T008 — `mock_data.py` + the persona contract**
  Files: `src/connectors/mock_data.py`, `tests/unit/test_mock_data.py`
  ~20 PRs in `ema/core`, ~20 Jira issues, module-level constants — **no randomness, no `datetime.now()`**.
  **Decision:** the persona counts are asserted against the raw constants *before any adapter exists*. If
  they drift, the RLS demo silently stops being 3→1 and nobody finds out until Phase 2's gate.
  Tests (same task): alice **3**, bob **1**, carol **0** `In Progress` issues linked to an **open** PR in
  `ema/core`; negative rows present (closed PRs, `Done`/`To Do` issues, issues with no PR) so a predicate is
  never vacuously true; every `reporter_email` distinct; importing twice yields identical objects.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_mock_data.py -v`

- [x] **T009 — pagination strategies** · gate: `test_pagination`
  Files: `src/connectors/pagination.py`, `tests/unit/test_pagination.py`
  `CursorStrategy` (github) and `OffsetStrategy` (jira, `startAt`/`total`), each computing the next token;
  placement stays a `RequestOption` (strategy ⊕ placement, Card 2).
  **Decision:** `next_cursor` carries the token; we do **not** format a literal `Link:` header. The header is
  a transport detail of a real GitHub call, and no mock makes one — the *decoupling* is the pattern worth
  proving, not the string.
  **Decision:** the cursor is an opaque **encoded** token, not a raw row index, so Phase 2 cannot accidentally
  build a cursor by hand and couple itself to the mock's internals.
  Tests (same task): `limit=5, page=None` → 5 rows, `has_more=True`, a `next_cursor`; the next page continues
  with **zero overlap** and is asserted against the full expected set; the last page has `has_more=False` and
  `next_cursor=None`; the stop condition `returned < page_size` holds for both strategies.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_pagination.py -v`

- [x] **T010 — both adapters**
  Files: `src/connectors/github.py`, `src/connectors/jira.py`, `tests/unit/test_connectors.py`
  `fetch()` implementing the six steps **in the locked order**: cache → token → secret → filter → paginate →
  record. Capability models per the phase file (github: `repo` required path param, `state`/`author`
  optional; jira: `status`/`assignee`/`project` `=`, `updated` with `=,>,>=,<,<=`).
  **Decision:** a predicate the capability model does not declare is **rejected, not silently ignored**.
  Silently dropping it would return rows the caller did not ask for — and in Phase 2 that predicate may be
  the RLS filter, which makes a silent drop a data leak rather than a bug.
  Tests (same task): `repo` omitted → raises (it is `required`); an undeclared operator → raises; declared
  predicates filter exactly; `AdapterResponse.served`/`fetched_at`/`etag` populated; both adapters return
  deterministic rows for the canonical predicates.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_connectors.py -v`

- [x] **T011 — the fetch-order invariant**
  Files: `tests/unit/test_fetch_order.py`
  **Decision:** this gets its own file because every other test asserts an *outcome*, and an outcome test
  cannot distinguish "cache hit, no token spent" from "token spent, then refunded". This one asserts the
  *order* with a limiter spy.
  Tests: on a cache hit the limiter is **never called**; on a miss it is called exactly once, and the secret
  is resolved **after** the token is consumed; a `304` spends no token.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_fetch_order.py -v`

## Config + seeding

- [x] **T012 — the YAML config**
  Files: `config/connectors/github.yaml`, `config/connectors/jira.yaml`, `config/grants.yaml`,
  `config/policies.yaml`, `config/rate_limits.yaml`
  Budgets exactly as the phase file specifies: `tenant_acme/github 5,60,2` (deliberately tiny — it is the
  429 demo), `tenant_acme/jira 30,60,5`, `tenant_load/*` `5000,60,500`.
  **Decision:** the RLS predicate is authored as a **JSONB AST** (`{"op":"eq","col":"assignee","value":":user"}`),
  never a SQL string (ADR-008, locked).
  Verify: `.venv/bin/python -c "import yaml,glob;[yaml.safe_load(open(f)) for f in glob.glob('config/**/*.yaml',recursive=True)]"`

- [x] **T013 — `scripts/seed.py`**
  Files: `scripts/seed.py`, `tests/integration/test_seed.py`
  Reads `config/*.yaml` → upserts `connectors`, `tenant_connector`, `secrets`, `policies`,
  `rate_limit_policies`. Encrypts each mock token with that tenant's `fernet_key` **at seed time**.
  **Decision:** every write is `ON CONFLICT DO UPDATE`, so `make seed` is re-runnable — a reviewer who
  reseeds after a demo must not get a crash or doubled rows (ADR-021).
  **Decision:** the seeder **reads** `tenants` and fails loudly if a tenant is missing; it never inserts one.
  Tenants are `002_seed_tenants.sql`'s, and a seeder that could create them would let the two disagree.
  Tests (integration, same task): seeding twice leaves identical row counts; `get_capabilities`,
  `get_policies` and `get_rate_limit_policy` read back exactly what the YAML declared; a missing tenant is a
  loud failure, not a silent skip (LAW 4).
  Verify: `make up && make seed && make seed && .venv/bin/python -m pytest -q tests/integration/test_seed.py`

- [x] **T014 — make the container able to seed**
  Files: `Dockerfile`, `Makefile`
  `COPY config/ ./config/` and `COPY scripts/ ./scripts/`; `make seed` becomes
  `docker compose exec -T app python -m scripts.seed`.
  **Decision:** seeding runs **inside** the container because Postgres and Redis publish no host ports, and
  opening one just for seeding would weaken the compose file for a build-time convenience.
  Verify: `make up && make seed` → prints a per-table row count and exits 0; `docker compose exec -T app ls config scripts`

## Polish

- [x] **T015 — make the README true**
  Files: `README.md`
  **Decision:** Phase 1 owns no *new* README section, but the Quickstart currently instructs the reader to run
  `make seed`, which prints `seed: no-op`. A quickstart that lies is a gate-6 failure ("if any step needs a
  human to explain it").
  Changes: Quickstart reflects real seeding; **Current status** → Phase 1 of 5, naming what still returns
  empty (`/v1/query`, until Phase 2); one line that the retry/circuit-breaker machinery in design-doc §5 is
  **described, not built** (#11), so a reviewer does not assume it runs.
  Verify: read it end-to-end once; `make up && make seed` following only the README, on a clean clone.

---

## Verification gate

The phase is **not** done until every line below passes, regardless of checkbox state.

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | All 7 acceptance tests green | `make test` | 0 failures; the 7 gate tests present by name |
| 2 | Hermetic suite stays infra-free | `docker compose down && make test` | still green with **no** containers running |
| 3 | Seeding is real and idempotent | `make up && make seed && make seed` | exit 0 twice; identical row counts |
| 4 | Seed round-trips the control plane | `make test-integration` | `get_capabilities`/`get_policies`/`get_rate_limit_policy` return the YAML's values |
| 5 | Lint clean, no ignore list | `.venv/bin/ruff check src tests scripts` | `All checks passed!` |
| 6 | LAW 1 | `find src scripts -name '*.py' \| xargs wc -l \| sort -rn \| head -3` | largest < 400 |
| 7 | Phase 0 still green | `make test` includes `tests/unit/test_config.py` etc. | 115 Phase-0 tests still pass |
| 8 | Cold start still under the gate | `make down && time make up` | < 60s |

**End-to-end flow test** (not just per-task verification): from a clean `make down`, run
`make up && make seed`, then in a Python shell inside the container call `github_adapter.fetch()` for
`tenant_acme` twice with the same request — assert the second is `served="cache"` with the limiter's
`remaining()` **unchanged** — then drain the 5+2 budget with unique requests and assert the 8th raises
`RATE_LIMIT_EXHAUSTED` with a `Retry-After`. That exercises cache → bucket → secret → filter → paginate in
one path, which no single unit test does.

## Open question for the reviewer

**`config/grants.yaml` carries the mock connector tokens in plaintext** (they are encrypted into Postgres at
seed time, but sit readable in the repo). This matches how Phase 0 already ships dev Fernet keys in
`002_seed_tenants.sql`, and they are fake values for a mock connector — but it is worth one deliberate look,
since Security is 15% of the rubric. The alternative is generating them at seed time, which costs the
determinism that `test_secret_indirection` relies on. **Recommendation:** keep them, with a header comment in
the file saying exactly what they are and that no real credential ever belongs there.
