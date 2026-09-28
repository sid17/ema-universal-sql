# Phase 1 — Connectors + Governance

> **Goal:** deterministic mock connectors behind a uniform contract, plus the two governance primitives that make
> them multi-tenant-safe: the token bucket and the freshness cache. **Gate:** pagination exact; bucket drain → 429
> signal; cache hit within TTL + 304; cross-tenant cache isolation holds. No SQL yet — adapters are called directly.

## Deliverables
1. `BaseConnectorAdapter` + `GitHubConnectorAdapter` + `JiraConnectorAdapter` (mock).
2. Deterministic seed datasets + `002_seed.sql` (tenants, grants, secrets, policies, budgets).
3. `TokenBucketRateLimiter` (Redis, atomic).
4. `FreshnessCacheManager` (Redis, ETag/TTL).
5. `SecretsManagerClient` (Fernet).

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

### `002_seed.sql` — policies (JSONB AST) + budgets
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
- Redis key `ratelimit:{tenant_id}:{connector_type}:{user_id}` and the two coarser keys `…:{connector_type}` and `…:{tenant}` (three nested buckets, checked coarsest→finest).
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

## Done when
All five tests green and both adapters return deterministic rows for the canonical predicates, so Phase 2 can wire the SQL pipeline on top without touching connector internals.
