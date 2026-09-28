# Prototype Prior-Art — OSS Research (build-choice validation)

> **Purpose:** validate and harden the *prototype's* concrete library + pattern choices against real OSS, via the
> `github-research` skill (`gh` search → triage → `repomix`/shallow-clone deep-read → patterns-to-adopt/skip).
> Distinct from `./prior-art-notes.md`, which covered the *architecture* prior-art (StackQL/Steampipe)
> for the design doc. All four cards below are **High confidence** — grounded in actual code (sqlglot's APIs were
> runtime-executed). Findings folded into the phase specs are marked **[applied]**.

## Search keywords
`sqlglot` · `steampipe` · `stackql` · `federated query` · `duckdb federation` / `duckdb api` · `row-level-security` · `opa sql` · `airbyte connector` · `postgrest` · `hasura` · `token-bucket ratelimit`

## Finalists (deep-analyzed) & why
| Repo | ★ | Lang | Validates |
|---|---|---|---|
| tobymao/sqlglot | 9.6k | Python | SQLParser + Entitlement AST-rewrite + Planner pushdown (Phase 2) |
| airbytehq/airbyte-python-cdk | (canonical CDK) | Python | Connector SDK: capability/pagination/error-mapping (Phase 1) |
| buremba/universql | 206 | Python | DuckDB-as-federation + SQL interception (Phase 2/5) |
| holgi/fastapi-permissions | 652 | Python | FastAPI auth→entitlement DI wiring (Phase 0/2) |

*(StackQL, Steampipe, PostgREST, Hasura triaged but not re-deep-read: StackQL/Steampipe already covered in the design-doc research; PostgREST/Hasura are Haskell/Go and delegate RLS to Postgres — conceptually noted, no code to port.)*

---

## Card 1 — tobymao/sqlglot (⭐ 9.6k, Python) · **the parse/rewrite/plan substrate**
- **What it does:** pure-Python, dependency-free SQL parser/transpiler/optimizer. SQL → typed `exp.Expression` AST → rewrite → regenerate for 20+ dialects. Ships a real optimizer (`qualify`, pushdown, simplify).
- **Key APIs (runtime-verified):**
  - Parse: `sqlglot.parse_one(sql, read="duckdb", into=exp.Select)` (raises `ParseError`).
  - Walk/extract: `find_all(exp.Table|exp.Column|exp.Join)`; `exp.Table` → `.catalog/.db/.name/.alias`; `exp.Column` → `.name/.table`; `exp.Join` → `.side`, `.args["on"]`; order/limit at `tree.args["order"]/["limit"]`; projections at `tree.selects`.
  - RLS inject (no string concat): `tree.where(exp.EQ(this=exp.column("assignee","issue"), expression=exp.Literal.string(user)))` — `.where(append=True)` AND-s into any existing WHERE.
  - CLS mask: `proj.replace(exp.alias_(exp.func("MD5", exp.column("reporter_email","issue")), "reporter_email"))` — wrapping in `alias_` keeps the output column name stable.
- **⚠️ Two load-bearing findings that change the spec:**
  1. **`qualify(tree, schema, dialect="duckdb")` MUST run first.** It normalizes/quotes identifiers, expands `*`, and stamps every `exp.Column` with its owning table/alias. Without it, bare columns have empty `.table` and you *cannot* attribute a predicate to a source. **[applied → Phase 2]**
  2. **Do NOT reuse sqlglot's `pushdown_predicates`/`pushdown_projections`.** They push *within a single AST* (into FROM-subqueries/joins), not *out to independent physical connectors*. For per-source split, **extract manually**: flatten the top-level `exp.And` **recursively**, then group each leaf by `{c.table for c in pred.find_all(exp.Column)}`; cross-source predicates (the join condition) fall out as non-pushable — which is exactly the guard we want. **[applied → Phase 2]**
     - **⚠ Corrected by the Phase 0 spike (`spike/ast_spike.py`, sqlglot 30.20.0):** this card originally said `where.this.flatten()`. That is wrong once RLS is injected. `tree.where(pred, append=True)` routes through `exp.and_`, which wraps the existing WHERE in an `exp.Paren`; `flatten()` prunes at the paren and returns the whole nested AND as a *single* leaf, so the three canonical predicates never separate and the split yields **zero** pushable GitHub predicates. Recurse through `.unnest()`. Confidence on this card is otherwise unchanged — every other claim (mandatory `qualify()`, `(db, name)` mapping, `exp.EQ` + `.where()`, `proj.replace(alias_(func(...)))`) was runtime-confirmed by the spike.
- **Concrete gotcha:** `github.pull_requests` parses as `.db='github'`, `.name='pull_requests'`, `.catalog=''` — map connector/resource on **`(table.db, table.name)`**, not catalog. **[applied → Phase 2]**
- **Subset validator:** walk the AST and reject any node type outside the whitelist — catches `exp.Star`, `exp.Insert/Update/Create` (writes/DDL), arbitrary `exp.Func`, subqueries. **[applied → Phase 2]**
- **Skip:** the full `optimizer.optimize()` pipeline (it also merges subqueries / eliminates joins / simplifies — reshapes the query before dispatch); dialect `transpile` for the federated result (we execute in DuckDB directly). Call only `qualify` + our own logic.

## Card 2 — airbytehq/airbyte-python-cdk (Python) · **the connector SDK model**
- **What it does:** the CDK behind Airbyte's declarative connectors — a YAML manifest (validated against a ~180-component schema) instantiated into runtime objects (`DeclarativeStream` → `SimpleRetriever` = requester + record-selector + paginator).
- **Patterns to adopt:**
  1. **One `RequestOption` injection primitive** — `{inject_into: query|header|body|path, field_name|field_path}` — reused for the pagination token, the page size, *and* every pushed predicate. Cleaner than separate `requestToken/responseToken` fields. **Merge it into our capability model:** a capability = *predicate support* (`{require, operators}`) **+** *its `RequestOption`* (where the value gets injected). **[applied → Phase 1]**
  2. **Pagination = strategy (computes next token) ⊕ placement (RequestOption), decoupled.** Strategies: `CursorPagination` (with an explicit `stop_condition` template), `OffsetIncrement`, `PageIncrement`; universal fallback stop = "returned `< page_size`". **[applied → Phase 1]**
  3. **Error mapping = a match→action table**, not ad-hoc: action enum `{SUCCESS, RETRY, FAIL, IGNORE, RATE_LIMITED, REFRESH_TOKEN_THEN_RETRY}` + a `failure_type` `{config_error, transient_error, system_error}` + a `DEFAULT_ERROR_MAPPING` keyed by status code with per-connector overrides. **[applied → Phase 1]**
  4. **Header-driven backoff** — `WaitTimeFromHeader`/`WaitUntilTimeFromHeader` read `Retry-After`; cheap and exactly right for GitHub/Jira. **[applied → Phase 1]**
- **Skip (overkill for 6–10h):** the Jinja interpolation engine + `$parameters` inheritance; the 180-component schema + model codegen; `CompositeErrorHandler`, `REDUCE_PAGE_SIZE`/`RESET_PAGINATION`, window clamping; `SubstreamPartitionRouter`/`AsyncRetriever`; the `HTTPAPIBudget` *proactive* rate limiter (we prove fairness with our own token bucket; reactive `Retry-After` is the complement) and the OAuth multi-token rotation machinery (one bearer + one refresh path covers GitHub/Jira).

## Card 3 — buremba/universql (⭐ 206, Python) · **DuckDB-as-federation**
- **What it does:** a Snowflake wire-protocol proxy that intercepts SQL, uses Snowflake only as a metadata catalog, and executes on a local in-memory **DuckDB** reading Iceberg/Parquet from object storage.
- **Patterns to adopt:**
  1. **Register each source as a DuckDB relation, then run the (transpiled) SQL over the registered names** — DuckDB does the JOIN/sort/limit/project. Our API-row equivalent of their `CREATE VIEW … iceberg_scan(...)`: fetch post-pushdown rows into Arrow/pandas, `duckdb.register('github_pull_requests', arrow_table)` + `register('jira_issues', …)`, execute. **This is our FederationEngine, validated concretely.** **[applied → Phase 2]**
  2. **`pyarrow.Table` as the internal result currency** — cheap to hand to FastAPI and to re-register in DuckDB. **[applied → Phase 2]**
  3. **`:memory:` DuckDB per request; idempotent re-registration each query** — no manual teardown. (Their per-session on-disk files are over-engineered for us.) **[applied → Phase 2]**
  4. **Two-role split** — `ICatalog` ("where/what is the data") vs `Executor` ("run it"), registered by name — keeps the planner source-agnostic. Validates our `BaseConnectorAdapter` contract.
  5. **A single AST-inspection routing function** (`_must_run_on_catalog`-style) returning federate-vs-elsewhere; default = local DuckDB, enumerate exceptions. Validates our federate-vs-materialize decision as one function.
- **Skip:** Snowflake wire-protocol + `fakesnow` emulator; Iceberg/object-store/`iceberg_scan`/MotherDuck; **their coarse all-or-nothing pushdown** (they never push predicates to the remote — they read raw lake files; our value prop is the *opposite*: push filters/limits into each API call. Borrow only their AST mechanics, not their pushdown model — we extract real per-source predicates ourselves, see Card 1).

## Card 4 — holgi/fastapi-permissions (⭐ 652, Python) · **auth→entitlement DI (cautionary)**
- **What it does:** ports Pyramid's ACL model to FastAPI — answers a boolean *"does this user have permission X on this resource?"* and wires it into a route as a `Depends`. Despite the "row-level" tagline it is **object/instance-level, post-fetch** (the resource dependency *loads the object*, then `has_permission` reads its `__acl__`). No SQL, no query rewrite.
- **Verdict:** architecturally **opposed** to our invariant "compile entitlement INTO the plan, never post-filter." This is confirmation that reaching for an off-the-shelf perms lib would be the *wrong* choice — a good thing to be able to say.
- **Patterns to adopt (front-half only):**
  1. `configure_permissions(principals_func)` → `functools.partial` factory: pre-provision the `EntitlementEngine` with the `UserContext`/principals dependency once, expose an ergonomic `Entitle(...)` in routes. Clean FastAPI DI template. **[applied → Phase 2 note]**
  2. Principals-as-namespaced-strings (`user:bob`, `role:support`, `system:authenticated`) — a tidy normalization for `UserContext.roles`.
  3. Split `get_current_user` (JWT decode) from `get_active_principals` (identity→principals) as two chained deps.
- **Skip:** the entire resource-fetch-then-`has_permission` flow (it *is* the post-filtering we ban); `__acl__`-on-the-model; boolean allow/deny as the primitive (we *transform* the query, not gate a loaded object; and it has no CLS/column-mask concept at all).

---

## Net changes to the plan (summary)
1. **Phase 2 gets concrete + more correct:** mandatory `qualify()` first; manual per-source predicate split (NOT sqlglot's optimizer passes); `(db,name)` table mapping; AST-node-whitelist subset validator; RLS via `exp.EQ`+`.where()`, CLS via `proj.replace(alias_(func(...)))`.
2. **Phase 2 FederationEngine gets concrete:** `duckdb.register(name, arrow_table)` per source → run SQL over registered names; pyarrow as currency; `:memory:` per request.
3. **Phase 1 connector SDK gets sharper:** the `RequestOption` injection primitive merged into the capability model; pagination = strategy ⊕ placement with explicit stop; error mapping = action-enum + failure_type + default status→action map; `Retry-After` header-driven backoff.
4. **Phase 2 DI note:** wire `EntitlementEngine` via a `configure_permissions`-style factory; explicitly *not* an object-level perms lib (records why).
5. **Validation, not change:** the DuckDB-federation shape, the two-role adapter split, and the compile-into-plan stance are all confirmed by real projects.
