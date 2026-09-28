# Phase 1 — Connectors + Governance

> **Goal:** deterministic mock connectors behind a uniform contract, plus the two governance primitives that make
> them multi-tenant-safe: the token bucket and the freshness cache. **Gate:** pagination exact; bucket drain → 429
> signal; cache hit within TTL + 304; cross-tenant cache isolation holds. No SQL yet — adapters are called directly.

## Deliverables
1. `BaseConnectorAdapter` + `GitHubConnectorAdapter` + `JiraConnectorAdapter` (mock).
2. Deterministic seed datasets + `config/*.yaml` + `scripts/seed.py` (connectors, grants, secrets, policies,
   budgets). **Not a `.sql` file** — see correction 5.
3. `TokenBucketRateLimiter` (Redis, atomic).
4. `FreshnessCacheManager` (Redis, ETag/TTL).
5. `SecretsManagerClient` (Fernet).

> **Corrections applied 2026-09-28, after Phase 0 shipped.** Five things below were written before Phase 0
> existed and were wrong against what it built. Fixed here rather than forked into the spec, per
> `03-BUILD-PROCESS.md` step 3.
>
> 1. **Tenants are already seeded, and the `002_` slot is taken.** Phase 0 shipped `002_seed_tenants.sql` for
>    one reason: its tenant-status gate reads `tenants` on every request, so with that table empty every
>    request correctly 403'd and Phase 0's own gate could never pass. Phase 1 seeds everything else, and
>    must not re-seed or renumber tenants. (Correction 5 then removes the seed `.sql` file entirely.)
> 2. **One token-bucket key, not three nested ones.** The three-key design has no configuration source:
>    `rate_limit_policies` is keyed `(tenant_id, connector_type)` and carries no per-user budget row, so the
>    `…:{user_id}` bucket could only ever be given a made-up limit. `02-DEFINITION-OF-DONE.md` §3 independently
>    tiers nesting as COULD, noting the brief asks for *a* token bucket with burst. See the corrected
>    §TokenBucketRateLimiter.
> 3. **The acceptance list is seven tests, not "all five".** See §Done when.
> 4. **The limiter takes an injected clock.** Without one, `test_bucket_burst` must really sleep for a refill
>    interval (12s), and it runs in the `pytest -q tests/unit` commit hook. See `kickoff/v2/research-repos.md`.
> 5. **There is no seed `.sql` file at all.** Deliverable 2 named one while §Seed configuration below says, in
>    its own words, *"Authoring format is **YAML, loaded by the seeder**, not hand-written `INSERT`s"* — and
>    gives two reasons from the brief (lines 29 and 154). The YAML path wins; the SQL block below stays only
>    as an illustration of the resulting rows, which is what it already says it is. Three further reasons to
>    prefer it: secrets must be Fernet-encrypted **with each tenant's own key at seed time**, which static SQL
>    can only do by committing ciphertext; `make seed` must be re-runnable, whereas a migration runs once
>    against the `schema_migrations` ledger Phase 0 built; and onboarding a connector stays *one YAML file +
>    one adapter class*, which is the claim line 29 is graded on.
>
> Migrations stay schema-only from here (`001_init.sql`), plus the one tenant-row exception Phase 0 had to
> make (`002_seed_tenants.sql`, which documents why in its own header).

## Connector contract (`src/connectors/base.py`)
```python
# RequestOption: ONE injection primitive (adopted from airbyte-cdk — research Card 2).
# Says WHERE a value goes in the outgoing call; reused for predicates, page token, and page size.
@dataclass
class RequestOption:
    inject_into: str                 # "query" | "header" | "body" | "path"
    field: str                       # param/header/path name, e.g. "state" or "startAt"

@dataclass
class CapabilityModel:
    # per column: filterable? with which operators? required (path param) or optional?
    # AND its RequestOption — the capability is (predicate support) + (where to inject it).
    key_columns: dict[str, dict]     # {"repo":  {"require":"required","ops":["="],
                                     #            "option":{"inject_into":"path","field":"repo"}},
                                     #  "state": {"require":"optional","ops":["="],
                                     #            "option":{"inject_into":"query","field":"state"}}, ...}
    sortable: list[str]              # columns the source can ORDER BY
    # pagination = strategy (computes next token) ⊕ placement (RequestOption), decoupled:
    pagination: dict                 # {"strategy":"cursor|offset|page", "page_size":100,
                                     #  "token_option":{"inject_into":"query","field":"cursor"},
                                     #  "stop":"returned<page_size | no-next-token"}

@dataclass
class AdapterResponse:
    rows: list[dict]
    fetched_at: float                # epoch seconds — feeds freshness_ms
    served: str                      # "live" | "cache"
    etag: str | None
    next_cursor: str | None
    has_more: bool

class BaseConnectorAdapter(ABC):
    connector_type: str
    def capabilities(self) -> CapabilityModel: ...
    def health(self) -> dict: ...
    async def fetch(self, *, tenant_id: str, predicates: dict,
                    projection: list[str], page: str | None,
                    limit: int) -> AdapterResponse: ...
```

`fetch` is the single seam a future **live** adapter reimplements (HLD §7). Inside the mock it runs these steps
**in this order — the order is load-bearing**: (1) **check the freshness cache** — a hit returns immediately and
spends **no** token; (2) consume a token via the limiter; (3) resolve the tenant secret via
`SecretsManagerClient` (proves indirection); (4) apply the pushed-down `predicates` to the in-memory dataset;
(5) paginate; (6) record `served`/`fetched_at`/`etag`.

> **Cache before token, never token before cache.** Spending a token on a cache hit breaks three things at
> once: the "`304` refreshes `fetched_at` without spending a token" guarantee below becomes false, the Phase-3
> `rate_limit_banner` spec stops being deterministic, and the Phase-4 load run drains `tenant_acme`'s GitHub
> bucket on request 6 instead of serving 30k requests from cache.

## Capability models (seed into `connectors.capabilities`)
- **github.pull_requests:** `repo` {required, `=`} (path param), `state` {optional, `=`}, `author` {optional, `=`}; sortable `[created_at, updated_at]`; cursor pagination (Link-header style). Columns: `title, author, repo, state, issue_key, created_at, updated_at`.
- **jira.issues:** `status` {optional, `=`}, `assignee` {optional, `=`}, `project` {optional, `=`}; `updated` {optional, `=,>,>=,<,<=`}; sortable `[updated]`; offset pagination (`startAt`/`total`). Columns: `key, status, assignee, reporter_email, project, updated`.

## Error mapping & backoff (`src/connectors/errors.py`) — adopted from airbyte-cdk (Card 2)

> **Partially built.** Per the tiering table below, Phase 1 builds the **vocabulary** — the action enum,
> `failure_type`, and a small mapping — because Phase 2 must tell a timeout (→ `partial`) from an auth
> error (→ fail) from a throttle (→ `429`), and that distinction *is* `failure_type`. It does **not** build
> the **machinery**: no circuit breaker, no exponential backoff, no retry loop. The mock makes no HTTP
> call, so a status→action table would map statuses that never occur and a breaker would guard a function
> that cannot fail transiently (LAW 5). The paragraph below describes the production design; the README
> says so explicitly.

A source's native failure is normalized through a **match→action table**, not ad-hoc `try/except`:
- **Action enum:** `SUCCESS | RETRY | RATE_LIMITED | REFRESH_TOKEN_THEN_RETRY | FAIL | IGNORE`.
- **`failure_type`:** `transient_error | config_error | system_error` (drives whether it's retryable and what caller message to emit).
- **`DEFAULT_ERROR_MAPPING`** keyed by HTTP status, with a per-connector override list: `429 → RATE_LIMITED`, `5xx/network → RETRY (transient)`, `401/403 → FAIL (config_error → CONNECTOR_AUTH_ERROR)`, `2xx → SUCCESS`.
- **Backoff:** on `RETRY`, bounded exponential + jitter; on `RATE_LIMITED`, **read `Retry-After` from the response header** (airbyte's `WaitTimeFromHeader`) and, if it exceeds the request deadline, surface `RATE_LIMIT_EXHAUSTED`. Retries wrap a per-connector **circuit breaker**. The final mapped result becomes the shared vocabulary (`RATE_LIMIT_EXHAUSTED`, `SOURCE_TIMEOUT`, `CONNECTOR_AUTH_ERROR`, …). *(For the mock, the forced-timeout/forced-429 hooks drive these paths deterministically; proactive per-endpoint budgets are the token bucket below, not airbyte's `HTTPAPIBudget`.)*

## Seed datasets (deterministic — `src/connectors/mock_data.py`)
- **GitHub** ~20 PRs in `ema/core`: fields `title, author, repo, state, issue_key, updated_at`. Ensure a stable subset is `state='open'` AND `issue_key` points at an `In Progress` Jira issue (so the canonical query has a known non-empty answer). Include some closed PRs and some pointing at non-in-progress issues (negative rows).
- **Jira** ~20 issues: fields `key, status, assignee, reporter_email, project, updated`. Assignment is the RLS
  demo, so make the three personas *visibly different, not empty-vs-nonempty*: `alice` → 3 `In Progress` issues
  linked to open PRs (e.g. `SUP-12, SUP-13, SUP-14`), `bob` → exactly 1 (`SUP-21`), `carol` → 0. Mix of
  `In Progress` / `Done` / `To Do` across the rest, each with a distinct `reporter_email`.
- **Personas:** `alice`, `bob`, `carol` (all role `support`, all real users — RLS differentiates purely by
  `assignee`). **alice 3 rows / bob 1 row** is the RLS demo: a row count that *shrinks* proves the filter, where
  a count that drops to zero looks like a broken query. **carol 0 rows** is the separate `empty` leg of the
  trichotomy (Phase 2).
- **Tenants:** `tenant_acme` (full seed) + `tenant_globex` (its own secret + a distinct cached row, used only by
  the isolation test) + `tenant_load` (same data as acme, but a large rate-limit budget — the k6 tenant; a 5-req
  budget and a 500-QPS load test cannot coexist on one tenant).

## Seed configuration — `config/*.yaml` → Postgres
Authoring format is **YAML, loaded by the seeder**, not hand-written `INSERT`s. Two reasons, both from the
brief: design-doc §6.2/§8.2 promise the minimal policy config "ships as YAML in the repo" (line 154), and
line 29 asks that admins onboard connectors "via console or **config**". A YAML loader satisfies both, so
onboarding a connector becomes *one file + one adapter class* rather than a migration.

- `config/connectors/github.yaml`, `config/connectors/jira.yaml` — `version` + the `CapabilityModel` below →
  loaded into `connectors(connector_type, version, capabilities JSONB)`.
- `config/policies.yaml` — the 1 RLS + 1 CLS rule → loaded into `policies` (predicate stays a JSONB AST,
  never a SQL string).
- `config/rate_limits.yaml` — budgets → `rate_limit_policies`.

The SQL below is the *resulting rows*, shown so the shape is unambiguous:

### The resulting rows — policies (JSONB AST) + budgets

*Illustration only. These rows are produced by `scripts/seed.py` from `config/policies.yaml` and
`config/rate_limits.yaml`; there is no hand-written seed migration (correction 5).*
```sql
INSERT INTO policies (policy_id, tenant_id, connector_type, resource, kind, applies_to, effect, predicate) VALUES
 ('rls-jira-assignee','tenant_acme','jira','issues','RLS','support','allow',
  '{"op":"eq","col":"assignee","value":":user"}');
INSERT INTO policies (policy_id, tenant_id, connector_type, resource, kind, applies_to, effect, column_name, mask) VALUES
 ('cls-jira-reporter','tenant_acme','jira','issues','CLS','support','allow', 'reporter_email','hash');
-- Two budget profiles, because one cannot serve both demos:
INSERT INTO rate_limit_policies VALUES
 ('tenant_acme','github',    5,   60,  2),   -- deliberately tiny: drains in a short loop -> the 429 demo
 ('tenant_acme','jira',     30,   60,  5),
 ('tenant_load','github', 5000,   60, 500),  -- k6 tenant: must NOT throttle (see Phase 4)
 ('tenant_load','jira',   5000,   60, 500);
```

## TokenBucketRateLimiter (`src/governance/ratelimit.py`)
- **One** Redis key, `ratelimit:{tenant_id}:{connector_type}` — matching the `rate_limit_policies` primary
  key exactly, so every bucket's capacity comes from a real row rather than an invented per-user limit.
  (Correction 2 above; the nested `…:{user_id}` / `…:{tenant}` buckets are COULD-tier and documented as a
  production extension, not built.)
- **Takes a clock** (`now_ms()`), defaulting to wall-clock epoch milliseconds — wall, not monotonic, because
  bucket state lives in Redis and is compared across processes. Tests inject a fake clock and advance it.
- Atomic refill+consume via a single Lua script (`tokens = min(cap, tokens + elapsed*rate); if tokens>=1 then tokens-=1`); returns `(allowed: bool, remaining: int, retry_after_ms: int)`.
- On empty → the adapter raises `ApiError(RATE_LIMIT_EXHAUSTED, http=429, retry_after_ms=…)`.
- Exposes `remaining(tenant, connector)` for the envelope's `rate_limit_status` and the `/metrics` gauge.

## FreshnessCacheManager (`src/governance/cache.py`)
- Key `cache:{tenant_id}:{entitlement_scope}:{connector}:{sha1(normalized_request)}`. **`tenant_id` + `entitlement_scope` are mandatory segments** — this is the entitlement trap; a test asserts they're present.
- Value `{fetched_at, etag, data}` with a **fixed server TTL** (`CACHE_TTL_MS`, default 300 000) — *not* the
  query's `max_staleness_ms`. TTL is a property of the **write**, staleness a property of the **read**: if a
  `max_staleness_ms=0` request wrote a zero-TTL entry, no later request could ever get a cache hit, and the
  Phase-3 freshness demo would be unreproducible. Staleness is enforced on read by `get()` below.
- `get(key, max_staleness_ms)`: hit within staleness → return `served="cache"`. Stale-but-present → caller may issue a conditional request (mock supports `If-None-Match` → returns `304` when the seeded `etag` matches) which refreshes `fetched_at` **without** consuming a token. Miss/changed → caller does a live fetch.
- Single-flight guard (asyncio lock per key) to collapse concurrent identical misses.

## SecretsManagerClient (`src/governance/secrets.py`)
- `resolve(secret_ref) -> token`: look up `secrets.ciphertext` by `secret_ref`, Fernet-decrypt with that tenant's `tenants.fernet_key`. Never returns another tenant's secret.
- Seed: encrypt a mock token per `(tenant, connector)` and store under a `secret_ref` referenced by `tenant_connector`.

## Acceptance tests (gate)
- `test_pagination`: `github_adapter.fetch(limit=5, page=None)` → 5 rows + `has_more=True` + a `next_cursor`; next page continues without overlap.
- `test_bucket_drain`: consume past `max_requests` → `RATE_LIMIT_EXHAUSTED` with a positive `retry_after_ms`;
  `remaining()` reads 0.
- `test_bucket_burst`: **the brief names burst explicitly (line 158), so it needs its own assertion.** With
  `max_requests=5, burst=2`, a cold bucket admits `5+2` requests back-to-back (the burst allowance is spent
  first), then throttles; after `window_sec/max_requests` of refill exactly one more is admitted. Without this
  test `burst` is a column in a table, not a behaviour.
- `test_cache_hit`: two fetches of the same request within TTL → second `served="cache"`, no token spent; after TTL → conditional request; matching etag → `304` refreshes `fetched_at`, still no token spent.
- `test_cross_tenant_isolation`: seed a `tenant_globex` cache entry for the same normalized request → `tenant_acme` fetch does **not** read it (different key); assert the key contains both tenant + scope.
- `test_secret_indirection`: `tenant_acme` GitHub fetch resolves `tenant_acme`'s token; a `tenant_globex` `secret_ref` decrypts to a different token; no cross-load.
- `test_crypto_shred`: destroy (delete) `tenant_globex`'s `fernet_key` → decrypting its stored `secrets.ciphertext` now raises (data is unrecoverable), while `tenant_acme`'s secret still decrypts. Backs the HLD §2 crypto-shred claim at prototype scale: offboarding = key-destroy, not a row scrub. (The gateway-reject half — `tenant.status='offboarding'` → refuse new requests — is a one-line check in Phase 0's coarse gate.)

## Scope tiering of this phase (resolving the overlap with `02-DEFINITION-OF-DONE.md` §3)

The sections above describe Phase 1 **in full**. DoD §3 independently tiers six of those details as COULD.
That is not a contradiction to resolve by picking a winner — it is the tiering `/spec` is required to add.
Recorded here so the gate below is honest about what it does and does not assert:

| Detail | Tier | Built in Phase 1? | Why |
|---|---|---|---|
| Two pagination strategies (github cursor, jira offset) | **MUST** | **yes** | Brief line 19 names pagination; `test_pagination` is the gate. Only literal `Link:` header *string formatting* is skipped — `next_cursor` carries the token. |
| ETag / `304` conditional revalidation | COULD | **yes** | It is in this phase's gate line and in `test_cache_hit`, and the "`304` refreshes `fetched_at` without spending a token" guarantee is load-bearing for the Phase-3 freshness demo. ~20 lines against an in-memory dict. |
| `test_crypto_shred` + `tenant_globex` beyond the isolation test | COULD | **yes** | ~15 lines, and `tenant_offboarded` is already seeded. Backs an HLD §2 claim that is otherwise prose only. |
| Error **vocabulary**: action enum + `failure_type` + mapping | COULD | **yes, minimal** | Phase 2 must distinguish timeout (→ `partial`) from auth error (→ fail) from throttle (→ `429`); that distinction *is* `failure_type`. A consumer one phase away, so not speculative. |
| Error **machinery**: circuit breaker, exponential backoff, retry loop | COULD | **no** | The mock makes no HTTP call, so a status→action table would map statuses that never occur and a breaker would guard a function that cannot fail transiently. LAW 5. README prose instead. |
| Three nested token buckets | COULD | **no** | Correction 2 above — no configuration source exists. |
| Single-flight coalescing guard | COULD | **no** | Revisit at Phase 4 only if the k6 run actually shows a thundering herd on `tenant_load`. |

## Done when

All **seven** acceptance tests above are green and both adapters return deterministic rows for the canonical
predicates, so Phase 2 can wire the SQL pipeline on top without touching connector internals.

Two of the seven (`test_cache_hit`'s `304` leg, `test_crypto_shred`) assert COULD-tier behaviour that the
table above commits to building. If either is cut later, cut the assertion and the feature together — a gate
that asserts less than it claims is worse than a smaller gate.
