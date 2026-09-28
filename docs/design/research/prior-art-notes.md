# Prior-art notes: SQL-over-SaaS-APIs (StackQL + Steampipe)

> **TL;DR (3 lines)**
> **StackQL** = SQL engine that transpiles `SELECT/INSERT/...` into HTTP API calls against cloud/SaaS providers; provider surface is *declarative*, defined as OpenAPI + `x-stackQL-*` extensions in a git registry, executed via a companion `any-sdk` library; results land in an embedded SQLite for relational algebra + cross-provider joins.
> **Borrow:** OpenAPI-derived connector definitions, provider→service→resource→method→SQL-table mapping, client-authoritative-with-optional-server-pushdown predicate model, declarative pagination token semantics, per-provider pluggable auth, DAG planner materializing API responses into a real RDBMS for joins.
> **Go beyond:** both tools are *single-tenant desktop/CI tools* — neither has multi-tenancy, query-time RLS/CLS entitlement, per-tenant fairness/quota, credential isolation/crypto-shred, or shared-service rate-limit governance. That is our whole hard part.

Primary source is the locally cloned StackQL repo (`internal/`, `pkg/`, `docs/`). Note: the *actual request-execution brain* is a separate library `any-sdk` (github.com/stackql/any-sdk), NOT in this repo — StackQL imports it (`github.com/stackql/any-sdk/public/formulation`, `.../pkg/auth_util`). Several deep details (dialect translation, HTTP preparation) live there and are only referenced here.

---

## 1. Data model / schema mapping

- **Hierarchy = `provider → service → resource → method`, mapped onto SQL.** `stackql` "generalizes ... into a `provider`, `service`, `resource` hierarchy that can be queried with SQL semantics" — `docs/high-level-design.md:16`. A resource ≈ a SQL table; a method ≈ the API operation backing a SQL verb.
- **Provider definitions are declarative, OpenAPI-derived.** "StackQL provider interfaces are canonically defined in OpenAPI extensions to the providers' specification. These definitions are then used to generate the SQL schema and the API client." — `README.md:128`. Definitions live in a separate git repo, the **StackQL Provider Registry** (`github.com/stackql/stackql-provider-registry`), downloaded/cached as "provider discovery documents" (`docs/high-level-design.md:29`).
- **Concrete config shape** (real example, `test/registry/src/stackql_native_test/v0.1.0/services/paged.yaml`):
  - Standard OpenAPI 3.0.1 (`openapi`, `servers`, `components.schemas`, `paths`) — the schemas define column types.
  - `x-stackQL-resources:` block maps resource → methods. Each resource has `id`, `name`, `title`, `methods.{select|insert|...}` and `sqlVerbs` that `$ref` into the methods (`paged.yaml:51-87`). A method's `operation.$ref` points at an OpenAPI `paths` entry; `response.objectKey: items` says which JSON field holds the row array (`paged.yaml:56-63`).
  - `x-stackQL-config:` block carries engine directives, e.g. `pagination` (see §5).
- **Provider-level YAML** exists too (`test/registry/.../provider.yaml`) declaring the provider name/version and splitting services into files (`services-split/...`).
- **Capability model (which fields filterable/sortable/pageable) is split three ways:**
  1. *Required* filters = params the API operation demands (HTTP path/query params). WHERE clauses that map to path params support **exact match only** — "Do not use wildcards or inequalities for WHERE clauses that map to parameters (eg: HTTP path parameters); in such cases, only exact matches are supported." — `AGENTS.md:109`.
  2. *Optional server pushdown* = declared per-method via a `queryParamPushdown` config; only present if the provider def opts in (`internal/stackql/pushdown/pushdown.go:7,28`).
  3. *Pageable* = declared via the `pagination` config (§5).
  - There is **no single unified "key column" capability object** like Steampipe's `KeyColumn`; capability is spread across OpenAPI params + `x-stackQL-config` directives.
- Discovery is introspectable at runtime: `SHOW PROVIDERS;`, `SHOW SERVICES IN <provider>;`, `DESCRIBE <resource>;` (`AGENTS.md:45`).

## 2. Architecture

- **Self-described as an amalgam of 5 things** (`docs/high-level-design.md:6-13`): (1) SQL parser+rewriter, (2) an ORM between arbitrary HTTP APIs and a traditional RDBMS, (3) a **planner for a DAG of API calls + their dependencies**, (4) an executor for that DAG, (5) a handle to a real SQL RDBMS to provide SQL semantics.
- **SQL parser = a fork of Vitess's `sqlparser`** (lex/yacc-style grammar) — vendored as `github.com/stackql/stackql-parser` (`docs/high-level-design.md:34`, `README.md:360`). Plans are "optimized and cached a la vitess" (`high-level-design.md:23`).
- **RDBMS backend = embedded SQLite**, specifically **pure-Go `modernc.org/sqlite`** (no cgo; `CGO_ENABLED=0` for all builds) — `AGENTS.md:63-76`. In-memory by default, optionally persistent (`high-level-design.md:53-54`). Postgres-over-TCP is also supported as a backend (`docs/data_flow.md:12`).
- **Server mode speaks the Postgres wire protocol** (via forked `psql-wire`), so any `psycopg2`/psql client connects — `README.md:124,364`. Also runs as a stdand-alone CLI (`exec`/`shell`) and as an **MCP server** (`README.md:159-167`).
- **Compilation pipeline (frontend→backend)** — `high-level-design.md:20-34`:
  1. **Lexical/syntax analysis** (Vitess) → AST.
  2. **Semantic analysis** → builds symbol table, resolves provider hierarchy + which APIs are needed (downloading/caching discovery docs), type-checks, creates a `Planbuilder`, emits at least a `Plan` stub.
  3. **Planning**: a `Plan` = a **DAG of `Primitive`s**; matured through intermediate-codegen → optimization (parallelize independent ops, remove redundant ops) → codegen (final backend calls).
  4. **Execution**: `Primitive` interfaces encapsulate access/mutation against arbitrary backends (http, SDK, IPC) — can be heterogeneous within one plan.
- **Component map (internal/stackql/ dirs, ~40 packages)** — notable: `parser`/`docparser`, `astanalysis`/`astvisit`, `planbuilder`/`plan`, `dependencyplanner` (DAG across tables/providers), `primitive`/`primitivegraph`/`primitivebuilder` (execution units), `provider` (HTTP/auth per provider), `pushdown` (predicate intent), `drm` (data-resource-mapping = JSON↔row + prepared statements), `router` (parameter/table routing), `psqlwire` (PG wire), `acid`/`txncounter`/`garbagecollector` (txn + GC), `responsehandler`, `data_staging`, `tableinsertioncontainer`. `HandlerContext` carries DB connection + user context through the pipeline (`high-level-design.md:44`).
- **Foreign-system semantics are deliberately pushed into `any-sdk`** (a separate lib) — "concrete types and foreign-system semantics belong behind interfaces, ideally in any-sdk" (`AGENTS.md:13,34`). This is a clean plugin-boundary design even though it's not a process boundary.

## 3. Data flow: `SELECT ... WHERE ...` → HTTP → rows

- **Trace:** AST → semantic analysis resolves `provider.service.resource` and selects the backing **method** (`internal/stackql/methodselect`, `provider/generic.go:142 GetMethodForAction`) → WHERE predicates that map to **required API params** are routed into the request (path/query params) by `internal/stackql/router/parameter_router.go` → provider makes the HTTP call(s) → response JSON's row-array (at `response.objectKey`) is extracted (`responsehandler`, `data_staging`) → rows are **INSERTed into a lazily-created SQLite table** → the *original* SQL (WHERE/projection/ORDER BY/LIMIT) is executed **authoritatively in SQLite** and streamed back.
- **Tables are lazily created per API response type**; table names are dot-namespaced `provider.service.resource.schema.generation` where `generation` is a monotonically-increasing version int (`high-level-design.md:56-57`). Every table carries **control columns identifying the query + session** the rows belong to — "to support audit, concurrency, and garbage collection" (`high-level-design.md:59-60`, and `txncounter`/`acid` packages). This is the key trick: fetched data is staged into a versioned relational store, and SQL runs over the stage.
- **Predicate pushdown is an OPTIMIZATION, not the correctness path** (`internal/stackql/pushdown/pushdown.go`):
  - Client-side WHERE/projection/LIMIT stay **authoritative**; "a partial or absent translation can never change results" (`pushdown.go:5-8`). So the API filter is best-effort; SQLite re-filters.
  - `ComputeIntent` returns nothing unless the method declares `queryParamPushdown` (`pushdown.go:24-32`).
  - Only a **"simple resource-scoped scan"** is pushable: single table, no DISTINCT/GROUP BY/HAVING/JOIN (`pushdown.go:135-143`). Anything else → LIMIT/predicates revert to client-side primitives.
  - Pushable predicates: AND-conjoined simple comparisons only; `=, !=, >, >=, <, <=`, plus `LIKE 'prefix%'` → a `startswith` predicate. OR / non-column LHS / other operators are silently dropped and left to client-side filtering (`pushdown.go:170-211`).
  - **Correctness guard worth stealing:** a pushed projection is *unioned* with every column referenced in WHERE/ORDER BY, else the authoritative client-side re-filter would drop all rows (issue #682) — `pushdown.go:66-69,102-131`.
  - `COUNT(*)` pushes the WHERE + the count but never limit/offset/projection/orderby (would misreport) — `pushdown.go:52-54`.
  - The neutral `PushdownIntent` is handed to `any-sdk`'s `HTTPPreparator.WithPushdownIntent`, which owns the **dialect-specific** translation (e.g. OData `$filter` vs GraphQL vs provider-specific query params) — `pushdown.go:1-8`. StackQL stays protocol-agnostic; the connector owns dialect.

## 4. Auth & secrets

- **Per-provider auth is configured via a single `--auth` JSON blob** keyed by provider, e.g. `--auth='{"aws":{"type":"aws_assume_role",...},"google":{"type":"service_account",...}}'` (`docs/auth.md:72,94`; `README.md:319-327`).
- **Auth type is dispatched per provider** in `provider/generic.go:102 Auth(...)` → a big switch over types: `api_key`, `bearer`, `service_account` (Google OAuth SA), `oauth2` (incl. `client_credentials` grant), `basic`, `custom`, `azure_default`, `interactive` (gcloud OAuth), `aws_signing_v4`, `null` (`generic.go:72-136`). Actual token mint/signing is delegated to `any-sdk/pkg/auth_util` (`AuthUtility`).
- **Secrets are injected by indirection, not stored.** Convention: config carries env-var *names* (`keyIDenvvar`, `credentialsenvvar`, `*_env_var`) and the engine reads the actual secret from the environment at request time; a literal value or an `*_env_var` name is accepted and the env-var form wins (`auth.md:35`, `generic.go`). `AGENTS.md:57` states secrets are meant to flow via env vars / CLI args / orchestration secret stores, "rather than hard-coded."
- **Federated/cross-account patterns are first-class** (`docs/auth.md:63-119`): AWS `sts:AssumeRole` with `ExternalId`, Azure multi-tenant app + consent, GCP cross-org SA impersonation, and GitHub-Actions OIDC → cloud federation. Auth clones the context per call (`authCtx = authCtx.Clone()`, `generic.go:107`) — no shared mutable auth state.
- **Gap for us:** auth is a *global process* config (one `--auth` for the whole engine invocation). There is **no notion of per-tenant credential sets, per-request credential selection, or credential isolation between tenants** — see §7.

## 5. Rate limiting / concurrency / caching

- **Pagination is declarative, per-method** — this is the strongest reusable idea. `x-stackQL-config.pagination` (`paged.yaml:14-25`) declares:
  - `algorithm` (e.g. `page_number`; cursor/token strategies also exist — see `services/cursor.yaml`),
  - `requestToken` `{key, location}` (e.g. `page` in `query`),
  - `responseToken` `{key, location}` (e.g. `result_info.page` in `body`),
  - `responseTerminator` `{key, location}` (e.g. `result_info.total_pages`) — loop until terminator reached; **if terminator is absent the reader stops after one page** rather than looping forever (`paged.yaml:11-13`).
  - Defaults when not declared, hard-coded per provider in `provider/generic.go:405-481`: GitHub/Okta use the **`Link` response header** with `rel="next"` (a `DefaultLinkHeaderTransformer`) and request via bare URL; everyone else defaults to `pageToken` query param (request) / `nextPageToken` body attribute (response). Max-page-size element defaults to `maxResults` query param (`generic.go:398-403`).
- **Concurrency:** the planner *parallelizes independent operations* in the DAG optimization phase (`high-level-design.md:26`) and has DAG machinery (`primitivegraph/`, `asynccompose/`). But there is **no rate limiter, token bucket, throttle backoff, or concurrency-cap** in the codebase — grep for `ratelimit|throttl|semaphore` finds only DAG/test code, nothing enforcing API quotas. Rate-limit handling is effectively **absent / left to the API returning 429**.
- **Caching:** provider **discovery documents are downloaded and cached** (`high-level-design.md:29`). Query *result* caching is via the staged SQLite tables + `generation` versioning (data persists for a query/session; can opt into a persistent DB). There is **no response/HTTP cache with TTL** akin to Steampipe's per-connection cache. GC of staged tables/records is **not implemented** — only a proposed manual "collect all" (`high-level-design.md:62-72`).

## 6. Joins across providers

- **Cross-provider joins ARE first-class** — "Multi-provider queries are a first class citizen" (`high-level-design.md:16`).
- **Mechanism = federate-then-materialize, not push-down-join.** The `dependencyplanner` builds a **dataflow DAG** (`gonum` graph, edges = data dependencies — `data_flow.md:9`) over the tables in the query. Each table/resource is a vertex; each vertex is independently **acquired** (its API called) and its rows **INSERTed into the embedded SQLite**, then the **join itself is executed by SQLite** over the staged tables (`dependencyplanner/dependencyplanner.go:168-287`). So joins run in a real relational engine after fan-out fetches — no bespoke join operator.
- **Two vertex classes** (`dependencyplanner.go:181-287`): orphan/independent vertices (inDegree=0) acquired directly; `WeaklyConnectedComponent`s = groups with data dependencies (one table's output feeds another's WHERE/params), executed in topological order with per-edge streams (`stream_collection.go`). Note: at the time of this code, data-*dependent* tables with nonzero in/out degree in the simple path are still restricted (`dependencyplanner.go:189-193` returns "cannot currently execute data dependent tables..."); dependent joins go through the WCC path.
- **Complexity is bounded eagerly:** a `DataflowDependencyMax` runtime param caps edge count; over-complex queries **fail at analysis time**, not runtime (`dependencyplanner.go:230-235`, `data_flow.md:26,30`).
- **Views/subqueries/CTEs/user tables are modeled uniformly as "indirections"** and composed into the same DAG to arbitrary configurable depth (`data_flow.md:16-32`, `dependencyplanner.go:254-287`). Provider-doc "views" can even clobber WHERE args from outside (`data_flow.md:15`).

## 7. Reusable ideas + gaps (what to borrow vs where we go beyond)

**Borrow from StackQL / Steampipe:**
1. **Declarative, OpenAPI-derived connector definitions** in a versioned registry (StackQL `x-stackQL-resources`/`x-stackQL-config`; Steampipe's plugin `TableMap`). Generating SQL schema + API client from a spec keeps connectors data-not-code and community-extensible. — `README.md:128`, `paged.yaml`.
2. **`provider→service→resource→method` → SQL-table mapping** with runtime introspection (`SHOW`/`DESCRIBE`). — `high-level-design.md:16`.
3. **Client-authoritative WHERE with best-effort server pushdown** as a *pure optimization* — never a correctness dependency — plus the projection-union guard (fetch every column WHERE/ORDER BY touch). Protocol-agnostic intent + connector-owned dialect translation. — `pushdown.go`.
4. **Declarative pagination token semantics** (`requestToken`/`responseToken`/`responseTerminator` with `{key, location}`; Link-header vs body-token strategies) — cleanly captures per-API paging without connector code. — `paged.yaml:14-25`, `generic.go:405-481`.
5. **Steampipe's explicit capability model** (`KeyColumns` with `Require: Required|Optional` + supported `Operators`) — a *cleaner, more unified* declaration of "which columns are filterable and with what operators" than StackQL's spread-out approach; borrow this shape for the capability schema. — steampipe.io/docs/develop/writing-plugins.
6. **Materialize-then-join in a real relational engine.** Fan out per-source API fetches into a staging store (SQLite/columnar), let SQL do joins/aggregation. Bound complexity **eagerly at plan time** (`DataflowDependencyMax`). — `dependencyplanner.go`, `high-level-design.md`.
7. **Per-provider pluggable auth via indirection** (env-var references, not inline secrets; per-call credential clone; assume-role/OIDC federation as first-class). — `auth.md`, `generic.go:102`.
8. **Steampipe's FDW+gRPC out-of-process plugin boundary** (Postgres FDW ⇄ gRPC ⇄ plugin) is worth borrowing for *fault/security isolation* of connectors — StackQL keeps this only as a code boundary (`any-sdk`), not a process boundary.

**Where we must go beyond (neither tool solves these):**
- **Multi-tenancy.** Both are single-tenant, single-invocation tools (one `--auth` blob per process; one embedded DB per run). We need tenant as a first-class dimension across schema, credentials, cache, and staging.
- **Query-time entitlement (RLS/CLS).** Neither does row-level or column-level security enforced at query time per calling principal. StackQL's staged tables carry only query/session control columns for GC/audit (`high-level-design.md:59`) — not access control. This is our core differentiator.
- **Tenant fairness / quota / shared-service rate governance.** StackQL has **no rate limiter at all**; Steampipe has per-connection limiters but no cross-tenant fairness. A multi-tenant engine over rate-limited SaaS APIs needs per-tenant + per-connector token buckets, priority/queueing, and noisy-neighbor isolation.
- **Credential isolation + crypto-shred.** Secrets here are process-env indirections; there is no per-tenant secret vault, envelope encryption, or per-tenant key so a tenant offboard can crypto-shred their data. We must add this.
- **Caching with entitlement + TTL + invalidation.** Both cache naively (discovery docs / per-connection results); neither reconciles a shared cache with per-tenant entitlement (can't serve tenant A's cached row to tenant B). Cache keys must include tenant + entitlement scope.
- **Durable/governed staging + GC.** StackQL's GC is unimplemented (`high-level-design.md:62`); our multi-tenant stage needs lifecycle, retention, and tenant-scoped deletion.
- **Reliability at the connector boundary.** No retries/circuit-breakers/backoff on 429/5xx in StackQL. Production multi-tenant federation needs these per connector + per tenant.
