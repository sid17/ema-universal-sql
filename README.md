# Universal SQL — federated queries across enterprise apps

One SQL query joining **GitHub pull requests to Jira issues**, served through a single API with query-time
entitlement (RLS/CLS compiled into the plan), per-tenant rate limiting, staleness-controlled caching, and
honest partial-result degradation.

```sql
SELECT pr.title, pr.author, issue.key, issue.status
FROM   github.pull_requests pr
JOIN   jira.issues issue ON pr.issue_key = issue.key
WHERE  pr.repo = 'ema/core' AND pr.state = 'open' AND issue.status = 'In Progress'
ORDER BY issue.updated DESC
LIMIT 50;
```

> **What this repo is.** This is take-home deliverables **3 (the runnable prototype)** and **4 (this README)**.
> Deliverables 1 and 2 — the high-level design with diagrams, and the six-month execution plan — are the
> **Google Doc**. `docs/design/design-doc.md` is a *reference copy* kept here for traceability, not the
> submission.

---

## Quickstart

**Prerequisites:** Docker Desktop running, and `make`. Nothing else — no local Python needed to run the stack.

```bash
git clone <this-repo> && cd ema-assignment
cp .env.example .env          # defaults work as-is for local
make up                       # builds + starts app, postgres:16, redis:7; waits for /healthz
make seed                     # loads config/*.yaml into the control plane
```

`make seed` prints what it wrote and is **safe to re-run** — every write is an upsert:

```
seed complete:
  tenants                     4 rows     <- created by migration, not by the seeder
  connectors                  2 rows     <- config/connectors/*.yaml
  tenant_connector            6 rows     <- config/grants.yaml
  secrets                     6 rows     <- Fernet ciphertext, one key per tenant
  policies                    2 rows     <- config/policies.yaml (1 RLS + 1 CLS)
  rate_limit_policies         6 rows     <- config/rate_limits.yaml
```

`make up` is cold-to-serving in **under 60 seconds** on a warm image cache. When it returns:

```bash
curl -s localhost:8000/healthz
# {"status":"ok"}

# mint a persona token (alice sees 3 rows, bob 1, carol 0)
T=$(curl -s -X POST localhost:8000/v1/auth/mock-token \
      -H 'content-type: application/json' \
      -d '{"user":"alice","role":"support","tenant":"tenant_acme"}' | jq -r .token)

curl -s -X POST localhost:8000/v1/query \
     -H "Authorization: Bearer $T" -H 'content-type: application/json' \
     -d '{"sql":"SELECT 1","max_staleness_ms":60000}' | jq
```

### Make targets

| Target | What it does | Needs Docker? |
|---|---|---|
| `make up` / `make down` | start / stop the three-service stack | yes |
| `make seed` | load `config/*.yaml` into the control plane; re-runnable | yes |
| `make test` | the unit suite — fast, hermetic | **no** |
| `make test-integration` | the integration suite | yes — run `make up` first |
| `make demo` | print the envelope for alice / bob / forced-timeout | yes |
| `make e2e` | Playwright specs against the console | yes |
| `make load` | k6, ~500 QPS for 60s | yes |
| `make fmt` | `ruff format` | no |

**On the test split:** `make test` deliberately needs no containers, so a reviewer can run it on a fresh clone
while images are still pulling. Anything requiring live Postgres/Redis lives in `tests/integration/` and runs
via `make test-integration`. A failure there means the stack isn't up, not that the code is broken.

### A deliberate caveat about the mock identity provider

`POST /v1/auth/mock-token` mints a valid token for **any** tenant, role and scope set, with no
authentication. That is the point — it stands in for a per-tenant OIDC provider so the entitlement flow can be
demonstrated without standing up an IdP (HLD §2) — but it has a consequence worth stating plainly rather than
letting a reader discover it:

> **In this prototype the gateway's tenant and scope gates are demonstrable, not enforceable.** A caller can
> mint themselves any tenant and the `query:execute` scope. The gates are real code on the real request path,
> and they are what a production deployment would run behind a real IdP — but with the mock issuer in front of
> them, they cannot withstand an adversary.

What *is* adversarially meaningful, and stays so in production, is that **row- and column-level entitlement is
compiled into the query plan** rather than applied to fetched rows (Phase 2). That property does not depend on
who issued the token.

In production the mock issuer is replaced by the tenant's IdP, `/v1/auth/mock-token` does not exist, and token
verification moves to RS256 + JWKS. Claim *shape* already follows
[RFC 9068](https://datatracker.ietf.org/doc/html/rfc9068), so nothing downstream changes.

### Onboarding a connector is one YAML file plus one adapter class

Connectors are data, not code (take-home line 29 — admins onboard "via console or **config**"). A connector's
capabilities live in `config/connectors/<name>.yaml` and are loaded into `connectors.capabilities` at seed
time; the adapter class supplies only its dataset. The same loader carries the minimal policy config (line
154) that design-doc §6.2/§8.2 promises ships as YAML:

| File | Becomes |
|---|---|
| `config/connectors/github.yaml`, `jira.yaml` | the capability models — what each source can filter, sort and page, **and where each value is injected** |
| `config/policies.yaml` | 1 RLS rule + 1 column mask. The predicate is a **JSONB AST, never a SQL string** |
| `config/rate_limits.yaml` | per-tenant token-bucket budgets |
| `config/grants.yaml` | which tenants may use which connector, and the mock credential each points at |

**There is no credential anywhere in this repository.** `config/grants.yaml` says which tenant may use which
connector and *where* its credential lives (`secret_ref`) — never what it is. `scripts/seed.py` generates a
fresh random token per grant at seed time and writes only Fernet ciphertext, encrypted with the owning
tenant's own key. Re-running `make seed` rotates every one of them.

That is deliberately *not* solved by adding a Vault container. Vault would relocate the plaintext rather than
remove it — something still has to put the token into Vault, and Vault's own bootstrap root token would then
live in `docker-compose.yml`. A secrets system's root of trust has to come from outside it, and a demo repo
has no outside. Not having a credential at all is the only version with no literal anywhere. Vault is still
the production target for `secret_ref`; see *Production mapping* below.

### Current status

**Phase 1 of 5 is complete.** Phase 0 shipped the shell (containers, auth, the typed envelope, the
control-plane reads, the observability seam); Phase 1 adds the two mock connectors behind one contract plus
the three governance primitives — the token bucket with burst, the freshness cache, and per-tenant secret
resolution — all seeded from YAML.

**`POST /v1/query` still returns a valid but empty envelope shell.** Nothing calls the connectors through SQL
yet: parsing, entitlement compilation, planning and execution are Phase 2. Until then the adapters are
exercised directly by the test suite, which is where the 3 → 1 → 0 persona numbers are asserted today.

### What is described in the design doc but deliberately not built

One thing is worth naming here rather than letting a reviewer infer it from design-doc §5. The connector
**error vocabulary** is real — an action enum, a `failure_type`, and a mapping — because Phase 2 needs to tell
a timeout (degrade to a partial result) from an auth error (fail the query) from a throttle (429). The
**machinery** around it is not: there is no circuit breaker, no exponential backoff and no retry loop. These
adapters make no HTTP call, so a table keyed by HTTP status would map statuses that never arrive, and a
breaker would guard a function that cannot fail transiently. The forced-failure hooks
(`adapter.fail_next(...)`) drive those paths deterministically instead.

---

## What it proves — the five hard parts

<!-- Filled in Phase 2, once `make demo` exists. Each hard part gets one command and what passing looks like. -->

*Pending Phase 2.*

## Trade-offs and the join strategy

<!-- Filled in Phase 2. Covers federated vs materialised (take-home line 69), why DuckDB `:memory:` stands in
     for both of design-doc §4.4's join paths, and why entitlement is compiled into the plan rather than
     applied post-fetch. -->

*Pending Phase 2.*

## Production mapping — what is described, not built

<!-- Completed in Phase 4: one paragraph per remaining non-goal in HLD §7 (live connectors, async reroute,
     DRR scheduler, residency enforcement, spill-to-disk, admin console, real OIDC, k8s/Helm/Terraform).
     Secrets & keys is written below, in the phase that built it. -->

### Secrets and keys

The prototype resolves every connector credential by indirection — `tenant_connector.secret_ref` →
`secrets.ciphertext` → Fernet-decrypt with **that tenant's own key** — and no credential exists anywhere in
this repository (`scripts/seed.py` generates them at seed time). Two properties that matter in production are
real here and tested: a tenant's `secret_ref` can never be decrypted with another tenant's key, and because
the key is per-tenant and stored outside the ciphertext, **offboarding is a key destruction rather than a row
scrub** — one write renders that tenant's data permanently unreadable while every other tenant is untouched
(`test_crypto_shred`).

**What it does not prove.** `tenants.fernet_key` holds raw key material in the same database as the ciphertext
it decrypts, and those four dev keys are committed. Encryption at rest only helps when the attacker reaches
the data without reaching the key, so against the likeliest breach — a leaked backup, a stale snapshot, a
stolen replica — a single `pg_dump` yields both halves and this buys no confidentiality. It is obfuscation
plus a crypto-shred mechanism, not protection, and it is scoped that way on purpose.

**Production** (brief line 99 — *"Vault + cloud KMS (tenant-scoped); rotation and break-glass"*) uses envelope
encryption: this column stores a **wrapped** data-encryption key, while the key-encryption key lives in KMS or
an HSM and never touches the database. Everything above survives the swap — per-tenant keys, crypto-shred,
onboarding a tenant without a redeploy — and a database dump alone becomes useless. It is a change of
**custody**, not of design, which is why the prototype demonstrates the mechanism rather than the vault.

A note on why there is no Vault container here: adding one would relocate the plaintext rather than remove it.
Something must still write the token into Vault, and Vault's own bootstrap root token would then live in
`docker-compose.yml`. Every secrets system has a root of trust that must come from outside itself; a demo
repository has no outside. Generating credentials and holding nothing is the only version with no literal
anywhere (ADR-026).

### Everything else

*Pending Phase 4.*

## Screenshots — and what they prove

<!-- Filled in Phase 4: the trace waterfall, the k6 summary, a /metrics scrape, and the console. -->

*Pending Phase 4.*

## Repository access

<!-- Filled in Phase 4: read access granted to souvik-sen@ema.co and careers@ema.co (take-home lines 50/165). -->

*Pending Phase 4.*

---

## Layout

```
src/
├── main.py          FastAPI app factory + lifespan
├── config.py        typed settings from env
├── models/          the response envelope, request, UserContext, error vocabulary
├── gateway/         auth (mock JWT), routes, error handlers
├── control_plane/   Postgres access + TTL-cached reads + migrations
├── observability/   OTel stage spans + Prometheus registry + access log
├── sqlparse/        sqlglot parse + subset validation          (Phase 2)
├── entitlement/     RLS predicate + CLS mask compiled INTO the AST   (Phase 2)
├── planner/         pushdown split + projection-union guard    (Phase 2)
├── execution/       DuckDB federation + envelope assembly      (Phase 2)
├── connectors/      mock GitHub + Jira adapters, capability models, datasets
└── governance/      token bucket, freshness cache, per-tenant secrets, clock

config/              connector capabilities, policies, budgets, grants — all YAML
scripts/seed.py      loads config/ into the control plane (`make seed`)

docs/design/         the HLD, execution plan, definition of done, per-phase specs
tests/unit/          hermetic — this is what the pre-commit hook runs
tests/integration/   needs `make up`
```

## Design documents

| Document | What it covers |
|---|---|
| `docs/design/00-PROTOTYPE-HLD.md` | what the prototype must prove, and the provenance rails |
| `docs/design/01-EXECUTION-PLAN.md` | build order and the scope ledger |
| `docs/design/02-DEFINITION-OF-DONE.md` | the submission gate and scope tiers |
| `docs/design/03-BUILD-PROCESS.md` | the per-phase command loop |
| `docs/kickoff/v1/architecture.md` | 18 ADRs, all Accepted |
