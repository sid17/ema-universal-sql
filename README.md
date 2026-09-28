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
  tenant_connector            5 rows     <- config/grants.yaml
  secrets                     5 rows     <- Fernet ciphertext, one key per tenant
  policies                    3 rows     <- config/policies.yaml (1 RLS + 1 CLS + 1 deny)
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
     -d '{"sql":"SELECT pr.title, pr.author, issue.key, issue.status
                 FROM   github.pull_requests pr
                 JOIN   jira.issues issue ON pr.issue_key = issue.key
                 WHERE  pr.repo = '"'"'ema/core'"'"' AND pr.state = '"'"'open'"'"'
                        AND issue.status = '"'"'In Progress'"'"'
                 ORDER BY issue.updated DESC
                 LIMIT 50",
          "max_staleness_ms":0}' | jq
```

Then swap `alice` for `bob` and run it again: **3 rows becomes 1**, from byte-identical SQL. Or just run
`make demo`, which does all of that and more and tees the output to `docs/demo-output.txt`.

### Make targets

| Target | What it does | Needs Docker? |
|---|---|---|
| `make up` / `make down` | start / stop the three-service stack | yes |
| `make seed` | load `config/*.yaml` into the control plane; re-runnable | yes |
| `make test` | the unit suite — fast, hermetic | **no** |
| `make test-integration` | the integration suite | yes — run `make up` first |
| `make demo` | the scripted walkthrough — alice, bob, the staleness knob, a forced timeout; tees to `docs/demo-output.txt` | yes |
| `make test-mode` | recreate the app with `TEST_MODE=1`, enabling the `/v1/test/*` hooks | yes |
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

**Phase 2 of 5 is complete, and `POST /v1/query` runs the canonical query end to end.** Phase 0 shipped the
shell (containers, auth, the typed envelope, the control-plane reads, the observability seam); Phase 1 added
the two mock connectors behind one contract plus the three governance primitives; Phase 2 built the pipeline
that turns SQL into an entitled answer — parse, entitle, plan, federate, assemble.

Still to come: **Phase 4** (k6 load profile, the trace waterfall artifact, the `/metrics` scrape, the rest of
this README) and **Phase 3** (the UI console and Playwright specs). Both depend only on Phase 2. Phase 4 runs
first because it holds four MUST-tier deliverables while the console is SHOULD-tier — and `make demo` already
carries the demo, so a slip there cannot leave the submission without one.

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

One command each. A claim with no command behind it does not count.

| # | Hard part | Command | Passing looks like |
|---|---|---|---|
| 1 | **Query-time RLS / CLS entitlement** | `make demo` (steps 1–3) | Row count goes **3 → 1** for the same SQL — it shrinks but stays non-zero. `reporter_email` comes back as an MD5 digest with `masked: true`; the raw address appears nowhere. The Jira adapter received `assignee=<persona>`, so the forbidden rows were **never fetched**. |
| 2 | **Per-tenant fairness over rate limits** | re-run step 1 more than 7× with `max_staleness_ms=0` | `429 RATE_LIMIT_EXHAUSTED`, a `Retry-After` header, and a `suggested_action` naming the async path. `tenant_acme`'s GitHub budget is 5 + 2 burst, deliberately tiny so the demo is deterministic. Never a hang. |
| 3 | **Credential isolation** | `make test` (`tests/unit/test_secrets.py`) | Each tenant's `secret_ref` decrypts only under its own Fernet key. `test_crypto_shred` destroys one key and shows the ciphertext survive as permanently unreadable, with every other tenant untouched. |
| 4 | **Entitlement-aware caching / freshness** | `make demo` (step 3) | `sources[].served` flips `live` → `cache`, `stats.connector_ms` empties, and `rate_limit_status` is **not decremented** — a cache hit spends no token. `freshness_ms` reports the **stalest** contributor. |
| 5 | **Connector reliability + honest degradation** | `make demo` (step 4) | `partial: true`, `join_status: "incomplete"`, a `SOURCE_TIMEOUT` warning naming `jira`, and `next_cursor: null`. The surviving GitHub rows are **never** passed off as the joined answer. |

Plus the correctness point that is graded and easy to lose — **empty ≠ partial ≠ error**:
`tests/integration/test_trichotomy.py` asserts all three shapes side by side. carol returns
`rows: [], partial: false, join_status: "complete"` (a *correct* answer); a timed-out source returns
`partial: true, join_status: "incomplete"` (a *degraded* one); malformed SQL returns `400 INVALID_QUERY`
with no envelope at all.

`make demo` writes everything it printed to [`docs/demo-output.txt`](docs/demo-output.txt), so the artifact
survives the terminal scrollback.

### The one assertion that matters most

Every count-based test in this repository would also pass against an implementation that fetched everything
and filtered it in Python — which is precisely the design the document argues against. So the load-bearing
assertion is not a row count:

```python
# tests/unit/test_federation.py
assert fetched["jira"] == 3      # alice
assert fetched["jira"] == 1      # bob
```

The **fetch** shrinks with the persona (9 → 3 → 1 unfiltered → alice → bob), because the RLS predicate was
compiled into the plan before the pushdown split. GitHub stays at 14 for every persona, correctly — the rule
is on a Jira column. A post-filtering build reads 9 every time.

## Trade-offs and the join strategy

### Federated vs materialized

The brief asks for the join strategy to be documented (line 69). This prototype is **federated**: every query
fetches from the sources at request time and joins in-process. Nothing is pre-copied.

|  | Federated (built) | Materialized (designed, not built) |
|---|---|---|
| Freshness | bounded by `max_staleness_ms`, caller-controlled per request | bounded by sync lag, operator-controlled |
| Entitlement | evaluated per request against live policy | must be re-evaluated on a copy, or the copy becomes a second place data can leak |
| Source load | one call per query per source, cut by the cache | amortized — the big win at high QPS |
| Joins | limited by what fits in memory (128 MB spill threshold) | arbitrary, indexed |
| Failure mode | a slow source degrades the answer (`partial`) | a stale copy silently answers with old data |

The decisive argument for federating **this** workload is the third row of that table, not the first. A
materialized copy of another system's data has to carry that system's entitlement rules with it, forever, and
stay correct as they change. Every copy is a second place a permission bug can leak from. Federating keeps
exactly one evaluation point — the query plan — which is the same reason entitlement is compiled in rather
than applied after the fetch.

Where materialization wins is scale, and the design document covers it as the path forward (§4.4): hot,
slow-changing, heavily-joined sources get materialized behind the same interface, and the planner chooses.
The prototype does not build that because a second execution path with no second correctness proof is worse
than one honest one.

### Why the entitlement goes into the plan

The alternative every object-level ACL library offers — fetch the rows, then check each one — is banned
outright here. It is not a performance argument. If forbidden rows cross the source boundary at all, then the
source's own audit log records a read that should never have happened, the rows sit in this process's memory,
and every downstream bug becomes a disclosure. Compiling `assignee = 'alice'` into the WHERE clause before
the pushdown split means Jira is *asked* a narrower question. The research note on this is explicit: we took
`fastapi-permissions`' dependency-injection shape and rejected its core flow.

### Pushdown is an optimization, never correctness

Two rules make that literally true rather than aspirational:

1. **The engine re-applies every predicate.** The SQL executed in DuckDB is the whole entitled tree, including
   the predicates a source already applied. A pushed predicate is simply applied twice, which is free. There
   is no "residual predicate list" to drift out of sync with what each connector actually managed to do.
2. **The fetch is `projection ∪ WHERE ∪ ORDER BY ∪ join keys`.** Otherwise that authoritative re-filter would
   evaluate a predicate against a column that was never fetched and drop rows the caller was entitled to.

A connector that supports no filters at all therefore returns correct results — just slowly. That is the
property that makes adding a real connector a bounded piece of work.

### Other trade-offs worth naming

- **DuckDB `:memory:` per request.** No state to clean up and no cross-request contamination, at the cost of
  re-registering the Arrow tables each time. At this row count that cost is unmeasurable; at scale it is the
  first thing to revisit.
- **Arrow schemas come from the capability model, never inferred from rows.** Inference looks tidier until an
  empty result infers zero columns and DuckDB refuses to register the table — which is every zero-row path:
  a denied resource, an RLS match of nothing, a timed-out source in a partial answer.
- **The audit log stores normalized SQL** (literals replaced with `?`). Storing the raw text would write
  `WHERE reporter_email = 'dana@acme.com'` into the compliance table in plaintext — the exact value the CLS
  rule exists to withhold. The logging layer must not defeat the masking layer.
- **An audit write that fails is logged, not raised.** The query already succeeded and its rows are already
  correct; failing the response would turn a logging outage into a customer-facing one. A compliance posture
  requiring the two to be transactional needs a durable queue, not a synchronous INSERT.
- **`SELECT *` is rejected.** It defeats projection pushdown — the fetch set becomes every column the source
  has. Naming columns is the price of a federated engine that only moves what you asked for.

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
