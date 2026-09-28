# Phase 1 — Connectors + Governance — Design Spec

> **Source of truth.** `docs/design/phases/phase-1-connectors.md`
> is spec-grade already and **wins on any conflict**. This document adds the four things it does not carry:
> per-task MUST/SHOULD/COULD tiering from `02-DEFINITION-OF-DONE.md` §3, explicit file paths against the
> HLD §8 layout, the test list as named files, and which README section this phase fills.
>
> Five corrections were applied **to the phase file itself** before writing this spec, per
> `03-BUILD-PROCESS.md` step 3 ("the contradiction is a real bug in the phase file and gets fixed there
> first, not forked into the spec"). They are recorded as ADR-019…ADR-025 in
> [`docs/kickoff/v2/architecture.md`](../../kickoff/v2/architecture.md).

## Context

Phase 0 shipped the shell: containers, auth, the typed envelope, the control-plane read layer, observability.
`POST /v1/query` returns a valid but **empty** envelope. Phase 1 fills in what that envelope will eventually
describe — two deterministic mock connectors behind one contract, and the three governance primitives that
make them multi-tenant-safe.

**No SQL in this phase.** Adapters are called directly from tests. The SQL pipeline that calls them is Phase 2.

## Architecture

```
src/connectors/
├── base.py          RequestOption · CapabilityModel · AdapterResponse · BaseConnectorAdapter (ABC)
├── errors.py        Action enum · FailureType · the mapping        [vocabulary only — ADR-022]
├── pagination.py    CursorStrategy (github) · OffsetStrategy (jira)
├── mock_data.py     ~20 PRs + ~20 issues, deterministic, module-level constants
├── github.py        GitHubConnectorAdapter
└── jira.py          JiraConnectorAdapter

src/governance/
├── clock.py         now_ms() protocol · WallClock (default) · FakeClock (tests)   [ADR-019]
├── ratelimit.py     TokenBucketRateLimiter + the Lua script
├── cache.py         FreshnessCacheManager (key builder · get · set · 304 path)
└── secrets.py       SecretsManagerClient (Fernet, per-tenant key)

config/
├── connectors/github.yaml · jira.yaml     version + CapabilityModel → connectors.capabilities
├── grants.yaml                            tenant_connector rows + the mock token per (tenant, connector)
├── policies.yaml                          1 RLS + 1 CLS, predicate as JSONB AST                [ADR-008]
└── rate_limits.yaml                       budgets → rate_limit_policies

scripts/seed.py      reads config/*.yaml → upserts into Postgres, encrypting secrets at seed time [ADR-021]
```

**Not created:** no `003_seed.sql`. Migrations stay schema-only (ADR-021).
**`src/models/` is frozen** — Phase 1 imports `ApiError`, `ErrorCode` and `UserContext`; it does not edit them.

### The `fetch()` order is the phase's central invariant

```
fetch() → 1. cache read ──hit──> return served="cache"   (NO token spent)
              │ miss
              ├─ 2. consume token   ──empty──> ApiError(RATE_LIMIT_EXHAUSTED, 429, retry_after_ms)
              ├─ 3. resolve secret via SecretsManagerClient
              ├─ 4. apply pushed-down predicates to the in-memory dataset
              ├─ 5. paginate
              └─ 6. record served="live" / fetched_at / etag → cache write
```

Cache **before** token, never the reverse (ADR-024). The limiter models the downstream API's budget, and a
cache hit makes no downstream call.

## Tech stack + key decisions

| Decision | Pick | Why |
|---|---|---|
| Bucket time source | injected clock, wall epoch ms | ADR-019 — otherwise `test_bucket_burst` sleeps 12s in the commit hook |
| Bucket key | one composite `{tenant}:{connector}` | ADR-020 — the schema has no per-user budget row to configure a third bucket from |
| Seeding | YAML + `scripts/seed.py` | ADR-021 — brief lines 29/154; secrets need per-tenant encryption at seed time; `make seed` must re-run |
| Error handling | vocabulary yes, machinery no | ADR-022 — Phase 2 needs `failure_type`; a mock has no HTTP status to map |
| `CACHE_TTL_MS` | **60 000 → 300 000** | ADR-023 — at 60s the TTL and the demo's staleness knob are the same number, so hard part 4 becomes a race |
| Limiter placement | inside `fetch()`, not a route dependency | ADR-024 |
| Cache-key scope segment | required argument from day one | ADR-025 — a missing scope segment leaks rows across users |
| Unit-test Redis | `fakeredis[lua]`, running the **real** Lua | verified below |

### Verified before planning (Step 0 — real data, not assumptions)

`fakeredis[lua]` 2.38.0 + `lupa` 2.8 were installed and probed against a draft of the actual token-bucket Lua
via `redis.register_script()`. Result, with `capacity=7 (max_requests 5 + burst 2)`, `refill=12000ms`:

```
cold bucket, 8 back-to-back at t=0:
  req 1..7: allowed=1  (remaining 6,5,4,3,2,1,0)
  req 8:    allowed=0  remaining=0  retry_after_ms=12000
advance exactly one refill interval (+12000ms):
  allowed=1  → then immediately allowed=0, retry_after_ms=12000
```

That is `test_bucket_burst`'s assertion, passing, with no Redis server. **Why this matters:** the alternative
was a pure-Python reference implementation in unit tests and the Lua only in integration — two
implementations of the same arithmetic, where the unit test passes while production is wrong. That is
precisely the failure Phase 0's AST spike caught (a silent pushdown disable), and it is not worth repeating.

## Data contracts

**Dataset shape** (`src/connectors/mock_data.py`, module-level constants, no randomness, no `datetime.now()`):

```python
GITHUB_PULL_REQUESTS = [  # ~20, repo "ema/core"
  {"title": "...", "author": "...", "repo": "ema/core", "state": "open",
   "issue_key": "SUP-12", "created_at": "2026-09-20T10:00:00Z", "updated_at": "..."}, ...]

JIRA_ISSUES = [           # ~20
  {"key": "SUP-12", "status": "In Progress", "assignee": "alice",
   "reporter_email": "dana@acme.com", "project": "SUP", "updated": "..."}, ...]
```

**The persona contract the whole demo rests on** — asserted directly against the constants, before any
adapter runs, because if these counts drift the RLS demo stops being 3→1 and nobody notices until Phase 2:

| Persona | Role | `In Progress` issues assigned, linked to an **open** PR in `ema/core` | Proves |
|---|---|---|---|
| `alice` | support | **3** (`SUP-12`, `SUP-13`, `SUP-14`) | the unfiltered baseline |
| `bob` | support | **1** (`SUP-21`) | RLS **shrinks** the set — a non-zero count, so it cannot be confused with a broken query |
| `carol` | support | **0** | the `empty` leg of the trichotomy (Phase 2) |

Negative rows are required, not optional: closed PRs, PRs pointing at `Done`/`To Do` issues, and issues with
no PR — otherwise every predicate in the canonical query is vacuously true and `test_pagination` cannot
distinguish a working filter from no filter.

**Tenants** are already seeded (`002_seed_tenants.sql`): `tenant_acme`, `tenant_globex`, `tenant_load`,
`tenant_offboarded`. Phase 1 **must not** re-seed or renumber them.

## User flows

1. **Cache-hit flow.** `fetch()` → key built with tenant + entitlement scope → hit within `max_staleness_ms`
   → `served="cache"`, `remaining` unchanged. The assertion is *"no token spent"*, not just `served=="cache"`.
2. **Stale-but-present flow.** Past `max_staleness_ms`, inside TTL → conditional request with the stored ETag
   → mock returns `304` → `fetched_at` refreshes, still no token spent, `served="cache"`.
3. **Drain flow.** Repeat a *uniquely-keyed* fetch past `max_requests + burst` → `ApiError(RATE_LIMIT_EXHAUSTED,
   429)` with a positive `retry_after_ms` and the async-reroute `suggested_action` (already auto-filled by
   `ApiError`). `remaining()` reads 0.
4. **Isolation flow.** `tenant_globex` writes a cache entry for a byte-identical request; `tenant_acme` fetches
   and does **not** read it.
5. **Offboarding flow.** Destroy `tenant_globex`'s `fernet_key` → its stored ciphertext no longer decrypts,
   while `tenant_acme`'s still does.

## Features by phase-task, with tier

| # | Deliverable | Tier | Notes |
|---|---|---|---|
| 1 | `BaseConnectorAdapter` + capability models + both adapters | **MUST** | brief line 156 ("2 connectors, mocked is fine") |
| 2 | Deterministic datasets + persona counts | **MUST** | the demo's whole visible surface |
| 3 | Pagination: cursor (github) + offset (jira) | **MUST** | brief line 19. Literal `Link:` header formatting skipped — `next_cursor` carries the token |
| 4 | `TokenBucketRateLimiter` **with burst** | **MUST** | brief line 158; burst gets its own test or it is a column, not a behaviour |
| 5 | `FreshnessCacheManager` — TTL + staleness-on-read | **MUST** | brief line 159 |
| 6 | `SecretsManagerClient` (Fernet, per-tenant) | **MUST** | DoD §2 hard part 3 |
| 7 | YAML config + `scripts/seed.py` + real `make seed` | **MUST** | brief lines 29, 154 |
| 8 | Error **vocabulary** (`Action`, `FailureType`, mapping) | COULD → **built** | Phase 2 consumes `failure_type`; see ADR-022 |
| 9 | ETag / `304` conditional revalidation | COULD → **built** | in this phase's gate line and in `test_cache_hit` |
| 10 | `test_crypto_shred` + `tenant_globex` | COULD → **built** | ~15 lines; backs an HLD §2 claim that is otherwise prose |
| 11 | Circuit breaker · exponential backoff · retry loop | COULD → **not built** | the mock makes no HTTP call (LAW 5). README prose |
| 12 | Three nested token buckets | COULD → **not built** | ADR-020 — no configuration source |
| 13 | Single-flight coalescing guard | COULD → **not built** | revisit at Phase 4 only if k6 shows a herd on `tenant_load` |

## Test list (named files, so the plan can make them checkboxes)

Everything below is **hermetic** and lives in `tests/unit/` — the pre-commit hook runs `pytest -q tests/unit`,
and this phase's whole gate must stay inside it. `fakeredis` supplies Redis; `FakeClock` supplies time.

| File | Covers | Gate test |
|---|---|---|
| `tests/unit/test_mock_data.py` | persona counts 3/1/0; determinism across imports; negative rows exist | — |
| `tests/unit/test_connectors.py` | capability models; predicate filtering; required-vs-optional predicates | — |
| `tests/unit/test_pagination.py` | `limit=5, page=None` → 5 rows + `has_more` + `next_cursor`; page 2 continues **without overlap**; both strategies | **`test_pagination`** |
| `tests/unit/test_ratelimit.py` | drain → `RATE_LIMIT_EXHAUSTED` + positive `retry_after_ms` + `remaining()==0`; burst `5+2` then exactly-one-after-refill | **`test_bucket_drain`**, **`test_bucket_burst`** |
| `tests/unit/test_cache.py` | hit within TTL, no token spent; stale → `304` refreshes `fetched_at`, still no token; key carries tenant **and** scope | **`test_cache_hit`**, **`test_cross_tenant_isolation`** |
| `tests/unit/test_secrets.py` | per-tenant decrypt, no cross-load; key destroyed → raises, other tenant unaffected | **`test_secret_indirection`**, **`test_crypto_shred`** |
| `tests/unit/test_fetch_order.py` | the ADR-024 invariant: on a cache hit the limiter is **never called** | — |
| `tests/integration/test_seed.py` | `scripts/seed.py` against live Postgres; re-running is idempotent; `get_capabilities`/`get_policies`/`get_rate_limit_policy` read back what YAML declared | — |

`test_fetch_order.py` exists because the other tests assert *outcomes*; this one asserts the *order*, with a
limiter spy. An outcome test cannot tell "cache hit, no token" from "token spent, then refunded".

## README section this phase fills

Phase 1 has **no new section** in the phase-0 README-growth table. It has a different obligation: the
Quickstart is currently **not true**. It tells the reader to run `make seed`, which today prints
`seed: no-op — seeding lands in Phase 1`. Phase 1's gate includes:

- `make seed` genuinely loads connectors, grants, secrets, policies and budgets;
- the **Current status** paragraph moves from "Phase 0 of 5" to "Phase 1 of 5" and states what still returns empty;
- one line naming the deferred error machinery (#11) so a reviewer reading design-doc §5 does not assume the
  retry/breaker path runs.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| `CACHE_TTL_MS` change breaks Phase 0 | It touches **four** files that must move together: `src/config.py:27`, `.env.example:21`, `docker-compose.yml`, and `tests/unit/test_config.py:43` which asserts the default. One task, not four (ADR-023) |
| Container cannot run `scripts/seed.py` | The Dockerfile copies only `src/` and `migrations/`. It must also copy `config/` and `scripts/` — Postgres and Redis publish **no host ports**, so seeding runs inside the container |
| `pyyaml` is undeclared | It is present only transitively via `uvicorn[standard]`. Config loading is a first-class deliverable, so declare it explicitly; add `fakeredis[lua]` under `dev` |
| Cache-before-token silently regresses | `test_fetch_order.py` + the "no token spent" assertions |
| Fake clock reaches production | `WallClock` is the default; `FakeClock` is only reachable through a `conftest.py` fixture |

## Source artifacts

- Phase file (**wins on conflict**): `docs/design/phases/phase-1-connectors.md`
- ADRs: `docs/kickoff/v2/architecture.md` (ADR-019…025) · `docs/kickoff/v1/architecture.md` (ADR-001…018)
- Research: `docs/kickoff/v2/research-repos.md` · `docs/design/research/prototype-prior-art.md` Card 2
- Tiers: `docs/design/02-DEFINITION-OF-DONE.md` §3
