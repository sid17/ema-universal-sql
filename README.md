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
make seed                     # loads the deterministic mock datasets and policies
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
| `make seed` | load mock datasets, policies, rate-limit budgets | yes |
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

### Current status

Phase 0 of 5 is complete: containers, auth, the typed response contract, the control-plane read layer and the
observability seam. **`POST /v1/query` returns a valid but empty envelope shell** — parsing, entitlement,
planning and execution land in Phase 2, and the seed data that makes the RLS demo visible lands in Phase 1.

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

<!-- Filled in Phase 4. One paragraph per non-goal in HLD §7 (live connectors, async reroute, DRR scheduler,
     residency enforcement, spill-to-disk, admin console, real OIDC/Vault/KMS, k8s/Helm/Terraform), each
     pointing at where the full design covers it. -->

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
├── connectors/      mock GitHub + Jira adapters                (Phase 1)
└── governance/      token bucket, freshness cache, secrets, audit  (Phase 1)

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
