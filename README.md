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
`make demo`, which does all of that and more and tees the output to `docs/artifacts/demo/demo-output.txt`.

### Make targets

| Target | What it does | Needs Docker? |
|---|---|---|
| `make up` / `make down` | start / stop the three-service stack | yes |
| `make seed` | load `config/*.yaml` into the control plane; re-runnable | yes |
| `make test` | the unit suite — fast, hermetic | **no** |
| `make test-integration` | the integration suite | yes — run `make up` first |
| `make demo` | the scripted walkthrough — alice, bob, the staleness knob, a forced timeout; tees to `docs/artifacts/demo/demo-output.txt` | yes |
| `make test-mode` | recreate the app with `TEST_MODE=1`, enabling the `/v1/test/*` hooks | yes |
| `make e2e` | Playwright specs against the console | yes — *Phase 3, not yet built* |
| `make load` | k6 at ~500 RPS for 60s → `docs/artifacts/load/k6-summary.txt` | yes — no host k6 needed |
| `make trace` | one live query → `docs/artifacts/trace/trace-waterfall.txt` + `.svg` | yes — no host Python needed |
| `make scrape` | `GET /metrics` → `docs/artifacts/metrics/metrics-scrape.txt` | yes |
| `make artifacts` | all three of the above, plus `make demo` | yes |
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

**Four of five phases are complete.** Phase 0 shipped the shell (containers, auth, the typed envelope, the
control-plane reads, the observability seam); Phase 1 added the two mock connectors behind one contract plus
the three governance primitives; Phase 2 built the pipeline that turns SQL into an entitled answer — parse,
entitle, plan, federate, assemble; Phase 4 made the performance legible and produced the artifacts above.

Still to come: **Phase 3**, the UI console and its Playwright specs. It is the only SHOULD-tier phase, it
depends only on Phase 2, and `make demo` already carries the demo — so a slip there cannot leave the
submission without one. Phase 4 ran first because it held four MUST-tier deliverables.

**Tests:** 591 unit (hermetic, no Docker) + 100 integration (against the running stack).

### What is described in the design doc but deliberately not built

Every non-goal is mapped, one paragraph each, under **Production mapping** below. The shortest version: the
connectors are mocks behind a real seam, the identity provider is a stand-in, the fairness proof stops at the
token bucket, and the error *vocabulary* is real while the error *machinery* (breaker, backoff, retry) is
not — because these adapters make no HTTP call, so a breaker would guard a function that cannot fail
transiently.

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

`make demo` writes everything it printed to [`docs/artifacts/demo/demo-output.txt`](docs/artifacts/demo/demo-output.txt), so the artifact
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

One paragraph per non-goal in HLD §7. Secrets and keys gets the long treatment because it is the one with a
mechanism worth demonstrating; the rest follow under *Everything else*.

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

One paragraph per non-goal in HLD §7, each naming where the full design covers it. The point is not to
apologise for what is missing — it is that every gap is a **scoped decision with a known production
counterpart**, and none of them is a hole somebody forgot.

**Live connectors.** Both sources are mocks with deterministic datasets, simulated pagination, a forced-429
hook and simulated latency (GitHub 40ms, Jira 180ms). The seam a live adapter plugs into is one method:
`BaseConnectorAdapter.fetch(request) -> AdapterResponse`. Everything above it — the capability model, the
predicate pushdown, the freshness cache, the token bucket, the credential resolution — is already written
against that contract and would not change. A live GitHub adapter is roughly: an HTTP client, the same
`config/connectors/github.yaml` capability model it already reads, and the `RequestOption` injection this
repo already models (`inject_into: path|query`). What it would *add* is the failure machinery deliberately
left out below.

**The error machinery, as opposed to the vocabulary.** The vocabulary is real — an action enum, a
`failure_type`, and a mapping — because the pipeline has to tell a timeout (degrade to partial) from an auth
error (fail the query) from a throttle (429). The machinery around it is not: no circuit breaker, no
exponential backoff, no retry loop, no `Retry-After`-driven scheduling. These adapters make no HTTP call, so
a breaker would guard a function that cannot fail transiently and a status-code table would map statuses
that never arrive. With live connectors all four become necessary on day one.

**Async reroute.** A drained bucket fails fast with `429 + Retry-After`, and its `suggested_action` names the
async path — which returns `501`, deliberately not `404`, so a caller who follows the pointer finds a scoped
decision rather than what looks like a bug. Production is design §4.2's `202 + job_id`: the query is queued,
the caller polls or gets a webhook, and a long tail of expensive cross-source joins stops competing with
interactive traffic for the same budget.

**DRR fair scheduling and per-connector bulkheads.** The prototype proves fairness at the token-bucket layer:
one bucket per `(tenant, connector)`, so a tenant draining GitHub cannot touch another tenant's budget or its
own Jira budget (`test_ratelimit.py`). What it does not prove is fairness *within* a tenant — one user
monopolising their tenant's budget is possible here. That is a multi-worker scheduling concern (design §4.1)
with no meaning in a single process; it needs a deficit-round-robin queue in front of a shared worker pool,
which is a different program, not a bigger function. Related and also unbuilt: **per-connector concurrency
pools**, without which a queue of slow Jira calls can starve GitHub queries of workers (brief line 121's
head-of-line blocking). The only bound today is the per-source deadline.

**Residency enforcement.** `tenants.residency` and `deployment_mode` are seeded, read and audited, so the
data model carries the constraint. Enforcement — pinning storage and compute to a region, refusing a
cross-region fetch — is meaningless in one local Docker network. In production this is placement policy at
the scheduler and per-region control planes, not a column check.

**Spill to disk.** DuckDB joins in memory with `:memory:` connections. The design's 128MB spill threshold is
a documented path, not an exercised one; the datasets here are 14 and 9 rows. What *is* real is the reason
spill matters — `SELECT *` is rejected and the planner fetches `projection ∪ WHERE ∪ ORDER BY ∪ join key`
precisely so the working set stays as small as the query allows.

**Admin console for connector onboarding.** Onboarding is genuinely one YAML file plus one adapter class,
and `connectors.version` is a real column, so "onboard via config" (brief line 29) is built rather than
claimed. The *console UI* for it is not. That is a CRUD screen over a table that already exists.

**Real OIDC.** The mock IdP mints any tenant, role and scope with no authentication at all — stated plainly
above, because it means the tenant and scope gates are *demonstrable* rather than *enforceable*. Production
is per-tenant OIDC discovery, JWKS-based verification with key rotation, and SCIM for the role claims the
entitlement engine reads. The verification code path is already the right shape: signature, `aud`, `exp`,
and required claims are all checked, and forged tokens (`alg: none`, algorithm confusion, RS256-signed) are
rejected today. Only the key source changes.

**Vault and KMS.** Covered in full above — envelope encryption, a wrapped DEK in the column and the KEK in
KMS. A change of custody, not of design.

**Audit durability.** One synchronous `INSERT` per query, and a failed write is logged rather than raised
(the query already succeeded; failing the response would turn a logging outage into a customer-facing one).
Measured at **0.25ms p50**, so it is not a throughput concern at this scale — but it is a *durability* one:
a compliance posture requiring the trail to be transactional with the read needs a durable queue with
at-least-once delivery, not a fire-and-forget insert.

**Metric cardinality.** `rate_limit_remaining{tenant, connector}` is one series per pair — eight here, and
linear in tenant count in production. At real tenant counts that is exactly the label you do not put on a
gauge; it becomes a sampled top-N, a per-tenant recording rule, or a separate query-time API rather than a
scrape-time series.

**One worker.** The container runs a single `uvicorn` process, which is why the load numbers below are what
they are. Multiple workers scale it roughly linearly — measured at **~4× on eight workers** — and would
immediately break `/metrics`, because `prometheus_client`'s default registry is per-process, so a scrape
would return whichever worker answered. Production runs N workers behind a load balancer with
`PROMETHEUS_MULTIPROC_DIR`, or scrapes each worker separately. Kept at one deliberately: a coherent
`/metrics` and a legible demo are worth more here than a bigger number.

**k8s / Helm / Terraform / autoscaling.** `docker-compose` with three services. Production is the usual:
a Deployment per component with HPA on the query-latency SLO, the control plane as a managed Postgres,
Redis as a managed cluster, and the connector fleet scaled independently of the query engine because their
load profiles are unrelated. Design §5 covers it; none of it changes a line of `src/`.

## Artifacts — and what each one proves

Four files in `docs/`, three of them regenerated by one command: **`make artifacts`**.

| Artifact | Reproduce with | What it proves |
|---|---|---|
| `docs/artifacts/trace/trace-waterfall.txt` / `.png` | `make trace` | where the time went, per stage and per source |
| `docs/artifacts/load/k6-summary.txt` | `make load` | throughput and latency at the brief's ~500 QPS |
| `docs/artifacts/metrics/metrics-scrape.txt` | `make scrape` | the Prometheus surface, with real samples |
| `docs/artifacts/demo/demo-output.txt` | `make demo` | four of the five hard parts, end to end |

### The trace — "P95 was Jira, not the engine"

![trace waterfall](docs/artifacts/trace/trace-waterfall.png)

```
trace cb235e63…2def   total 204.6ms   spans 10

POST /v1/query         │████████████████████████████████████████████████│    204.6ms
  gateway              │▊██████████████████████████████████████████████▉│    202.9ms
    parse              │▍                                               │      1.5ms
    entitlement        │▏                                               │      0.1ms
    plan               │▏                                               │      0.1ms
    federation         │▍█████████████████████████████████████████████▊ │    196.5ms
      connector.github │▎██████████▌                                    │     45.7ms
      connector.jira   │▎███████████████████████████████████████████▌   │    186.5ms
      duckdb_join      │                                            ▍█▊ │      8.7ms
    assemble           │                                              ▎ │      0.9ms
```

**Three readings, in order of how much they matter.**

1. **The engine is not the cost.** Parse, entitlement, plan and assemble together are **2.6ms of
   205ms — under 1.3%.** Everything this prototype is *about* —
   validating the SQL subset, compiling an RLS predicate and a CLS mask into the AST, splitting predicates by
   owning source — is free next to one remote call. That is the argument for doing entitlement at plan time
   rather than worrying about its cost.
2. **The two connector bars overlap.** `connector.github` and `connector.jira` start together and run
   concurrently; the request costs `max(46, 187)`, not `46 + 187`. The spans are
   opened *inside* the `asyncio.gather` closure precisely so this is visible — a span synthesised afterwards
   from `stats.connector_ms` would carry invented start times and could only ever render as sequential.
3. **`duckdb_join` starts when the slower source finishes.** 187ms in, not before. That is the join
   waiting on the last fetch, which is what a federated engine looks like when it is working correctly.

There is **no tracing backend** here — no Jaeger, no OTLP collector, no fourth container. Spans go to
`traces/spans.jsonl` and `scripts/waterfall.py` (standard library only) renders them. `make trace` truncates
the log first, so the artifact provably describes the code that is checked out rather than something that
accumulated across earlier runs.

### The load test — the honest version

`make load` drives **500 RPS for 60s** at `POST /v1/query`, the rate the brief asks for, against
`tenant_load` (seeded at 5000 req/60s — pointing 30,000 requests at `tenant_acme`'s deliberately tiny 5/60s
budget would measure nothing but throttling). A second scenario then drains `tenant_acme`'s bucket and
asserts a clean `429`.

```
  target rate           500 RPS for 60s, tenant_load, max_staleness_ms=60000
  iterations completed  5057
  iterations dropped    24959
  achieved rate         60.2 RPS
  p(50)                 8229.5ms
  p(95)                 60000.8ms
  FAIL  http_req_duration{scenario:federated_query} p(95)<1500
  PASS  checks{scenario:drain_bucket} rate==1.0
```

**That threshold is red and it is staying red.** One `uvicorn` worker on a laptop does not hold 500 RPS, and
tuning the target down until the line went green would measure patience rather than the system. The number
that matters is `dropped_iterations`: k6 **skipped 83% of the requested work**, so the p(50) above describes
only the 17% it managed. A summary that omitted that figure would report a far healthier-looking system than
this one is.

**The capacity curve.** 15s per point, warm cache, the canonical two-source join:

| offered RPS | 1 worker | 8 workers |
|---|---|---|
| 50 | 49.2 · p50 20ms | 49.0 · p50 19ms |
| 100 | 61.1 · p50 **5,164ms** | 97.7 · p50 22ms |
| 200 | 67.8 · p50 **7,989ms** | **196.6 · p50 35ms · p95 259ms** |
| 400 | — | 239.0 · p50 3,748ms |

The knee is ~50–65 RPS on one worker and ~200–240 on eight. **At 200 RPS with eight workers the join query
is p50 35ms / p95 259ms** — inside the brief's SLO (P50 < 500ms, P95 < 1.5s) by 14× and 6×.

**What the run actually found.** The phase spec predicted the synchronous audit `INSERT` would dominate the
P95 at this rate. It does not — measuring took four minutes and said so:

| synchronous work, per request | p50 | share of what blocks the event loop |
|---|---|---|
| **DuckDB join** | **10.29ms** | **~95%** |
| audit `INSERT` | 0.25ms | ~2% |
| sqlglot parse | 0.24ms | ~2% |

The join was CPU-bound work running *on the asyncio event loop*, so it serialised every concurrent request
behind one core — a hard ceiling around `1000 / 10.3 ≈ 97 RPS` in theory and **48 RPS measured**. Moving it
to a worker thread (`asyncio.to_thread`; DuckDB releases the GIL, and each call already opens its own
`:memory:` connection) gave, at a matched 200 RPS target:

| | before | after |
|---|---|---|
| achieved rate | 48.3 RPS | **63.1 RPS** (+31%) |
| p(50) | 21,850ms | **6,767ms** (−69%) |
| p(95) | 29,147ms | **20,959ms** (−28%) |

The audit batching the spec asked for was **not built**, because the measurement said it would have bought
~2%: building it would have been an optimisation aimed at a hypothesis, inside the one phase whose entire
deliverable is evidence.

Two caveats worth stating rather than burying: the run sets `OTEL_EXPORTER=none`, so these numbers exclude
span-export overhead; and `max_staleness_ms=60000` means cache hits dominate by design, so this measures the
engine rather than the mocks' simulated latency.

**And the ceiling that matters in production is not this one.** Connector latency is `await` — wall-clock,
not CPU — so it sets concurrency, never throughput. But GitHub REST allows 5,000 requests/hour per token =
**1.39 req/s**. At 1k QPS across 100 tenants the required cache hit ratio is `1 − 139/1000` ≈ **86%**; for a
single tenant driving 1k QPS it is **99.86%**. You cannot serve 1k QPS of *fresh* data from a quota-limited
SaaS API, which is precisely why `max_staleness_ms`, the freshness cache and ETag revalidation exist.

### The metrics scrape

`docs/artifacts/metrics/metrics-scrape.txt` is a real `GET /metrics`, captured after a demo run:

```
rate_limit_remaining{connector="github",tenant="tenant_acme"} 5.0
rate_limit_remaining{connector="jira",tenant="tenant_acme"} 34.0
connector_fetch_duration_seconds_count{connector="github"} 6.0
connector_fetch_duration_seconds_count{connector="jira"} 6.0
query_duration_seconds_count 6.0
```

`rate_limit_remaining` is published from the same single read of the token bucket that fills the envelope's
`rate_limit_status`, so the number a caller sees and the number Prometheus publishes cannot drift.
`query_duration_seconds` is observed in the route in a `finally`, so it counts rejected and denied queries
too — a histogram that only saw successes would report a P95 better than the one callers actually get.

**A note on how this was tested, because it is the interesting part.** The gauge was declared in Phase 0 and
fed by nothing for two phases, and the integration test guarding it —
`assert "rate_limit_remaining" in body` — passed the entire time, because Prometheus emits a `# HELP` line
for a declared-but-never-recorded collector. Every assertion in `tests/integration/test_metrics.py` now
parses a **labelled sample with a numeric value**. A metric name in a scrape proves only that somebody
declared it.

## Repository access

> **Open item.** The repository is not yet pushed to a remote. Before submission, grant **read** access to
> `souvik-sen@ema.co` and `careers@ema.co` (take-home lines 50/165) and replace this block with the URL:
>
> ```
> gh repo create <org>/<name> --private --source=. --push
> gh api -X PUT repos/<org>/<name>/collaborators/<user> -f permission=pull
> ```
>
> Nothing else in this README depends on it.

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
├── execution/       parallel fetch, DuckDB join, envelope assembly (Phase 2)
├── connectors/      mock GitHub + Jira adapters, capability models, datasets
└── governance/      token bucket, freshness cache, per-tenant secrets, audit, clock

config/              connector capabilities, policies, budgets, grants — all YAML
load/query_load.js   the k6 profile — zero remote imports, so it runs offline (Phase 4)
scripts/seed.py      loads config/ into the control plane (`make seed`)
scripts/demo.sh      the scripted walkthrough (`make demo`)
scripts/trace.sh     capture one live query's trace (`make trace`)
scripts/waterfall.py render traces/spans.jsonl as a waterfall — stdlib only (Phase 4)

docs/                the four submission artifacts — see "Artifacts" above
traces/spans.jsonl   the span sink. Gitignored; truncated by `make trace`

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
| `docs/kickoff/v*/architecture.md` | 45 ADRs across four kickoff rounds, one per phase, all Accepted |
| `docs/kickoff/v*/research-repos.md` | the runtime probes each phase ran before designing — including the four gaps between a locked document and the code that Phase 4's probes found |

**One document is deliberately not here.** `docs/design/` holds a *reference copy* of the high-level design
for traceability; the submitted design document and six-month execution plan are the Google Doc. This
repository is deliverable 3 (the prototype) and deliverable 4 (this README).
