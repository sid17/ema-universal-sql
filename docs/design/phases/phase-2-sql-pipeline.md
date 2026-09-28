# Phase 2 — SQL Pipeline (parse → entitlement → plan → execute)

> **Goal:** the intellectual core. Turn the canonical SQL into entitled, pushed-down source fetches, join them in
> DuckDB, and assemble an honest envelope. **Gate:** canonical query returns entitled rows for alice; RLS visibly
> shrinks bob's rows (and the forbidden rows are never fetched); CLS masks `reporter_email`; timeout → partial;
> empty ≠ partial ≠ error. This phase depends on Phase 1 adapters and Phase 0 contracts.

> **Corrections applied 2026-09-28, before the Phase 2 spec was written.** Eight things below were
> written before Phases 0 and 1 existed, or before the runtime behaviour was measured, and were wrong
> against what was actually built. Fixed here rather than forked into the spec, per
> `03-BUILD-PROCESS.md` step 3. ADRs in [`../../kickoff/v3/architecture.md`](../../kickoff/v3/architecture.md);
> the measurements in [`../../kickoff/v3/research-repos.md`](../../kickoff/v3/research-repos.md).
>
> 1. **The node whitelist runs BEFORE `qualify()`** (ADR-027). Measured: `qualify()` *expands* `SELECT *`
>    against the schema, so `exp.Star` is gone by the time a post-qualify validator walks the tree and
>    `SELECT *` would be silently accepted. "Qualify first" stays true of column *attribution*, which is
>    all stages 3–5 need — it was never a claim about validation order. See §Stage 2.
> 2. **Malformed SQL has a name: `INVALID_QUERY`, HTTP 400** (ADR-028). The file said "→ 400" twice and
>    named no code; the six-code vocabulary has none that fits, and design-doc §8.1 is locked. It gets a
>    separate exception type on the `UnauthenticatedError` precedent — deliberately not a seventh code.
> 3. **`test_deny_overrides` contradicted this file's own prose** (ADR-031). It said a `deny` policy
>    yields *empty*; §Stage 3 and `test_entitlement_denied` say an explicit deny is **403**. The prose
>    wins — it matches the HLD §9 rail. The two tests are now split by level, not by outcome.
> 4. **Arrow tables are built from an explicit capability-derived schema** (ADR-030). Measured:
>    `pa.Table.from_pylist([])` infers zero columns and `duckdb.register()` then raises
>    `InvalidInputException`. Every zero-row path — denied resource, empty RLS match, timed-out source in
>    a partial — would have crashed the engine. See §FederationEngine.
> 5. **The per-request deadline must actually be enforced** (ADR-032). Phase 0 set
>    `request.state.deadline_ms` and nothing read it. `test_timeout_partial` as written drives only the
>    `fail_next` hook, so "timeouts → partial" would be proven by an exception we raise ourselves while a
>    genuinely slow source hung the request. See §FederationEngine.
> 6. **`audit_logs.query_text` stores normalized SQL** (ADR-033). Writing the raw text would put
>    `WHERE reporter_email = 'dana@acme.com'` into the audit table in plaintext — exactly the value the
>    CLS rule exists to keep from the caller. The logging layer must not defeat the masking layer.
> 7. **Two Jira rows need a tied `updated` value** (ADR-035). This file requires `test_pagination` to fail
>    if the `key` tiebreaker is missing, but `mock_data.py` has twenty distinct timestamps and no ties —
>    so the test would pass identically with the tiebreaker deleted. `SUP-13` and `SUP-14` are tied.
> 8. **`CONNECTOR_AUTH_ERROR` is 403, and Phase 1 raises 502** (ADR-029). This file was right; the built
>    code drifted from a locked HLD §9 rail. `src/governance/secrets.py` is corrected in this phase.

## Pipeline order (`src/pipeline/runner.py` — `QueryPipelineRunner`)
```
QueryRequest + UserContext
   → SQLParser.parse_and_validate         (stage 2)
   → EntitlementEngine.compile            (stage 3)   RLS filter + CLS mask INTO the plan
   → QueryPlanner.plan                    (stage 4)   pushdown split + projection-union guard
   → [per source] adapter.fetch(...)                  (Phase-1 adapters, in parallel)
   → FederationEngine.execute             (stage 5)   DuckDB join/sort/limit/project
   → assemble QueryEnvelope + AuditLogger.write
```

> **Ordering note (reconciles with design-doc §2.3 hop numbering).** The design doc numbers Planner as hop 2 and
> Entitlement as hop 3, but §3.2 requires RLS to "push down *with* the query" — which means entitlement must be
> injected into the logical plan **before** the pushdown split. So the prototype runs parse → **entitle** →
> **plan/pushdown**: the RLS predicate is AND-ed in first, then the planner pushes *all* predicates (RLS included)
> to the sources. This is consistent with the design doc, not a departure — the hop numbers are logical roles, not
> a claim that all pushdown precedes entitlement.

## Stage 2 — SQLParser (`src/sqlparse/parser.py`, sqlglot)
Concrete sqlglot usage (APIs runtime-verified — see `../research/prototype-prior-art.md` Card 1):
**Order: parse → reject → qualify** (CORRECTION 1, ADR-027). The whitelist must see the *raw* AST.

- **Parse:** `sqlglot.parse_one(sql, read="duckdb", into=exp.Select)`. The `into=` form raises
  `ParseError` on `INSERT`/`UPDATE`/`DELETE`/`DROP` before any walk (probe-confirmed), so DDL and DML
  are rejected for free. `ParseError` → `InvalidQueryError` (**400 `INVALID_QUERY`**, CORRECTION 2).
- **Qualify SECOND, and mandatory:** `tree = sqlglot.optimizer.qualify.qualify(tree, schema=<connector schemas>, dialect="duckdb")`. This stamps every `exp.Column` with its owning table/alias — *without it you cannot attribute a predicate to a source*, so every later stage depends on it. It also expands `*` and normalizes identifiers; pass the connector column schemas so `validate_qualify_columns` doubles as free validation.
- **Subset validator = AST node-type whitelist, run on the RAW tree before `qualify()`:** walk and reject any node outside {`Select, From, Join, Where, Order, Limit, Column, Table, Literal, EQ/NEQ/GT/…, And/Or, Alias`}. This catches `exp.Star` (reject `SELECT *`), `exp.Insert/Update/Delete/Create/Drop` (writes/DDL), arbitrary `exp.Func`, and subqueries — cleaner than string matching. Violation → top-level `400` (distinct from empty).
  **Two ordering facts, both of which must be commented in the code:**
  1. The whitelist runs **before `qualify()`**. Measured: `qualify()` expands `SELECT *` against the schema,
     so a post-qualify walk finds no `exp.Star` and accepts the one projection that defeats projection
     pushdown. (ADR-027.)
  2. The whitelist runs on the *user's* AST, before stage 3 injects `MD5(...)`. The entitlement engine's own
     nodes are trusted and never re-validated. Without that note a reviewer reads the CLS `MD5` injection as
     violating this rule's own ban on arbitrary functions.

  **Build the whitelist from the measured node set, not from the sketch above** — the canonical query, once
  qualified, contains `Identifier`, `TableAlias` and `Ordered`, which the sketch omits; a whitelist missing
  them rejects the canonical query itself. The constructs that must stay out, each with the node it emits:
  `IN (SELECT …)` → `In`/`Subquery`; `COUNT(x)` → `Count`; `UNION` → `Union`; `LIKE` → `Like`;
  `GROUP BY` → `Group`; `SELECT *` → `Star`.
- **Extract:** `tables` via `find_all(exp.Table)` mapped on **`(table.db, table.name)`** — note `github.pull_requests` parses as `.db='github'`, `.name='pull_requests'`, `.catalog=''` (map on db+name, NOT catalog); keep `.alias` for column attribution. `projection` from `tree.selects`; `predicates` by flattening `tree.find(exp.Where)`; `join_keys` from `exp.Join.args["on"]`; `order_by`/`limit` from `tree.args["order"]/["limit"]`.
- Coarse gate here too: every referenced connector must be enabled for the tenant (`tenant_connector`), else
  `CONNECTOR_NOT_ENABLED` (403). **This gate is load-bearing, not belt-and-braces:** measured, `qualify()`
  raises on an unknown *column* but silently accepts an unknown *table* — `SELECT s.a FROM slack.msgs s`
  passes the optimizer untouched. Nothing else stands between that query and the planner.

```python
@dataclass
class ParsedQuery:
    ast: exp.Expression
    tables: list[TableRef]                 # {connector, resource, alias}
    projection: list[ColumnRef]
    predicates: dict[str, list[Predicate]] # keyed by table alias
    join_keys: list[tuple[ColumnRef, ColumnRef]]
    order_by: list[tuple[ColumnRef, str]]
    limit: int | None
```

## Stage 3 — EntitlementEngine (`src/entitlement/engine.py`) — the crux
- Fetch applicable policies once: `SELECT … FROM policies WHERE tenant_id=? AND connector_type = ANY(?) AND resource = ANY(? || '*') AND (applies_to = ANY(roles) OR applies_to='*') AND enabled`.
- **RLS (concrete, pure-AST — no string concat):** resolve the JSONB predicate AST (`:user` → `UserContext.user_id`) into a sqlglot node and AND it into the plan via `tree.where(exp.EQ(this=exp.column("assignee","issue"), expression=exp.Literal.string(user)), append=True)` so it rides pushdown. Canonical: `jira.issues.assignee = <alice>`. `append=True` merges into any existing WHERE.
  **What to assert, and where:** the AST carries a **literal** (`assignee = 'alice'`). `assignee = currentUser()`
  is only how a *live* Jira adapter would render that predicate into JQL (design-doc §3.2) — it is a rendering
  detail of one connector, not the plan. So assert at the adapter's received-predicates layer
  (`predicates["assignee"] == "alice"`), never against a JQL string. The execution plan's Phase-2 prose says
  `currentUser()`; that is the same fact one layer down.
- **CLS (concrete):** for each CLS policy, rewrite the projection node in place — `proj.replace(exp.alias_(exp.func("MD5", exp.column("reporter_email","issue")), "reporter_email"))` for `hash`; `null`→`exp.Null()`; `redact`→a literal `'••••'`; `drop`→pop the column from `tree.selects` *and* from the source field request (column-pushdown saving). Wrapping in `exp.alias_` keeps the output column name stable. Register the mask on `(resource, column)`; `mask ∈ {null, hash, redact, drop}`.
- **deny-overrides + default-deny — two different outcomes, deliberately:**
  - An **explicit `effect='deny'`** matching a resource the query *references* → `403 ENTITLEMENT_DENIED`. The
    caller asked for something they are forbidden to see; saying "zero rows" would be a lie, and it would leave
    `ENTITLEMENT_DENIED` as a declared-but-unreachable code in the six-code vocabulary.
  - **No matching `allow`** for the user's roles (default-deny) → **empty**, not 403 and not open. The caller
    asked for something they simply have no grant for; an empty answer is the correct answer.
  This split is what makes `ENTITLEMENT_DENIED` (design-doc §4.5) actually reachable, and it keeps the `empty`
  leg of the trichotomy honest. Deny always overrides a matching allow.
- **Join-key masking ordering:** if a masked column is also a join key, apply the mask only in the *final* projection (after the join), never before — or the join breaks.
- Output: an `EntitledPlan` = `ParsedQuery` with RLS predicates merged in + a `masks` map + a `denied` set.

```python
@dataclass
class EntitledPlan:
    parsed: ParsedQuery          # predicates now include RLS
    masks: dict[str, str]        # "jira.issues.reporter_email" -> "hash"
    denied_resources: set[str]   # yield empty for these
    entitlement_scope: str       # feeds the cache key (Phase 1)
```
For the canonical `assignee=:user` rule, `entitlement_scope` = `user_id` (the resolved RLS binding), so the cache key is per-user and never leaks a's rows to b. Set-valued rules (*"issues in your team's projects"*) would resolve against a small `entitlement_scope` table — stubbed to `user_id` in the prototype.

**DI wiring (adopted from fastapi-permissions, front-half only — Card 4).** Expose the engine via a `configure_entitlement(get_current_user)` → `functools.partial` factory (the `configure_permissions` pattern), so routes get an ergonomic dependency and `UserContext` is injected once. We deliberately do **not** use fastapi-permissions itself or any object-level ACL lib: those *fetch the row then check it*, which is the post-filtering our invariant bans. We compile entitlement into the plan instead.

## Stage 4 — QueryPlanner (`src/planner/planner.py`)
- **Per-source split is manual (do NOT call sqlglot's `pushdown_predicates`/`pushdown_projections`).** Those optimizer passes push *within one AST* (into subqueries/joins), not out to independent connectors — wrong abstraction here (Card 1). Instead: flatten the top-level `exp.And` **recursively** and group each leaf predicate by `{c.table for c in pred.find_all(exp.Column)}`. **Spike correction (verified, `spike/ast_spike.py`): a single `where.this.flatten()` is NOT enough.** `tree.where(pred, append=True)` routes through `exp.and_`, which wraps the *existing* WHERE in an `exp.Paren` before AND-ing; `flatten()` prunes at the paren and yields the whole nested AND as one leaf, so after RLS injection the original predicates never separate and **nothing is pushable**. Recurse through `.unnest()` instead — see `_flatten_conjunction` in the spike. Single-table leaves → candidate pushdown for that source; multi-table leaves (the join condition) fall out as non-pushable and stay in-engine — exactly the guard we want.
- For each candidate, consult the connector's `CapabilityModel`: it's **pushable** only if the column is filterable *and* the operator is supported; else **residual** (kept for the engine). A required key column absent → error (`repo` is required on GitHub). Each pushed predicate carries its `RequestOption` (Phase 1) telling the adapter where to inject it.
- **Projection-union guard (invariant #1):** the set of columns fetched from a source = `projection(for that source) ∪ every column referenced in any WHERE or ORDER BY on that source`. Without this the authoritative in-engine re-filter could drop a valid row. Assert it in code + test.
- **Pushdown is optimization, never correctness:** whatever a source can't filter stays a residual filter the engine re-applies. The engine re-applies *every* predicate after fetch.
- **Operator caveats:** `LIMIT` is **not** pushed through the join (apply after join); `ORDER BY`+`LIMIT` over a join → re-sort in-engine before limit. A per-source loose bound may be pushed only when provably safe.
- Output a `QueryPlan`: per-source `{predicates_pushed, columns_to_fetch}` + the residual `{join, order_by, limit, residual_filters, masks}`.

## Stage 5 — Federation + assembly — **two modules, not one**
As originally specced this stage owned fetch orchestration, DuckDB execution, envelope assembly, freshness
computation, `join_status`/`partial`, and cursor pagination — six responsibilities, comfortably over 500 lines,
which violates LAW 1 (the pre-commit hook blocks it) and LAW 3. Split it:

- **`src/execution/federation.py`** — `FederationEngine`: call the adapters in parallel, register the arrow
  tables, run the residual SQL, return raw rows + the per-source `AdapterResponse`s. *Knows DuckDB, knows
  nothing about the envelope.*
- **`src/execution/assemble.py`** — `ResultAssembler`: envelope construction, `ColumnMeta` (incl. `masked`),
  `freshness_ms`, `join_status`/`partial`/`warnings`, and cursor pagination. *Knows the contract, knows
  nothing about DuckDB.*

### FederationEngine (`src/execution/federation.py`, DuckDB)
- Call each adapter's `fetch(...)` (Phase 1) **in parallel** with the pushed predicates + `columns_to_fetch`;
  collect `AdapterResponse`s.
- **Enforce the deadline here** (CORRECTION 5, ADR-032). Phase 0 set `request.state.deadline_ms` in
  `routes.py` and nothing ever read it. Each fetch is wrapped in `asyncio.wait_for(..., timeout=budget)`
  inside `asyncio.gather(..., return_exceptions=True)`, where the per-source budget descends from
  `REQUEST_TIMEOUT_MS` — which is what makes the `routes.py` docstring's "parent of the per-source budgets"
  claim true rather than aspirational. `return_exceptions=True` is load-bearing: the default cancels the
  sibling fetch the moment one fails, so a Jira timeout would throw away GitHub rows we already held and
  turn a `partial` answer into an `error` one — the trichotomy collapsing. A `TimeoutError` becomes
  `SourceOutcome(state="timeout")` + `partial=True`, never a 500.
- **Register + execute (concrete, validated by universql — Card 3):** open an in-memory DuckDB per request (`duckdb.connect(":memory:")`); convert each source's rows to a `pyarrow.Table` **built with an explicit `pa.schema(...)` derived from
  `columns_to_fetch`, never inferred from the rows** (CORRECTION 4, ADR-030) — measured,
  `pa.Table.from_pylist([])` infers zero columns and `con.register()` then raises
  `InvalidInputException: must have at least one column`, so every zero-row path (denied resource, empty RLS
  match, a timed-out source in a partial) would crash the engine; then `con.register("github_pull_requests", tbl)` / `con.register("jira_issues", tbl)`; run the residual query (join on `issue_key`, re-apply *every* residual filter authoritatively, `ORDER BY issue.updated DESC, issue.key ASC`, `LIMIT 50`, project entitled columns) over the registered names; `to_arrow_table()` back (**spike correction:** on duckdb 1.5.x `.arrow()` returns a `RecordBatchReader`, not a `Table`, and `fetch_arrow_table()` is deprecated). `pyarrow.Table` is the internal currency (cheap to register and to serialize); `:memory:` + idempotent re-registration means no teardown.
- **Masks are *not* re-applied here.** The CLS rewrite already happened in stage 3, in the projection AST, and is
  expressed exactly once — this stage merely *executes* that AST, so `MD5(reporter_email) AS reporter_email` runs
  as part of the final `SELECT`. That final SELECT is by definition post-join, which is what satisfies the
  join-key masking rule (design-doc §3.2) without a second code path. The only thing this stage adds is
  `ColumnMeta.masked = True` for every column in `EntitledPlan.masks`.
- **freshness_ms** = `int((now − min(r.fetched_at for r in responses)) * 1000)` — the stalest contributor. If it exceeds `max_staleness_ms` and no refresh was possible → append a `STALE_DATA` warning.
- **Pagination (functional req, take-home line 19):** after sort+limit, page the *joined result* — `next_cursor`
  is an opaque base64 offset over the sorted entitled rows (`{"offset": N}`); a follow-up request echoing the
  cursor returns the next window; `null` when exhausted. **A tiebreaker is mandatory:** the sort must be
  `updated DESC, key ASC`, because an offset cursor over a non-total order lets ties reorder between pages —
  rows get skipped or duplicated and `test_pagination` flakes. **`next_cursor` is always `null` when
  `partial=true`** — an offset into an incomplete result set is not stable, so we refuse to page it. `LIMIT` is applied after the join (never pushed through it). Distinct from connector-level pagination, which lives in the adapters (Phase 1).
- **join_status / partial:** if a joined side's adapter returned a timeout → `partial=True`, `join_status="incomplete"`, warning `{code:SOURCE_TIMEOUT, connector}`; return the driving side's rows, **never** the un-joined set passed off as joined. A non-join multi-source scan with one side missing → `partial=True`, `join_status="n/a"`.
- Assemble `QueryEnvelope`; **`AuditLogger` (`src/governance/audit.py`, built here)** writes one `audit_logs` row
  `{tenant, user, query_text, sources_accessed, rows_returned, trace_id, execution_ms}` — the compliance
  access-trail (design-doc §3.4). **`query_text` is the NORMALIZED SQL, literals replaced by `?`**
  (CORRECTION 6, ADR-033), produced from the AST we already hold rather than by a regex over the string.
  Writing the raw text would put `WHERE issue.reporter_email = 'dana@acme.com'` into the audit table in
  plaintext — the exact value the CLS rule exists to keep from the caller. The logging layer must not
  defeat the masking layer.

## The three outcomes (must stay distinct — HLD §5)
| Outcome | Trigger | Envelope |
|---|---|---|
| **Empty** | ran fine, 0 rows after filters/entitlement | `rows:[]`, `partial:false`, `join_status:"complete"` |
| **Partial** | a source timed out/throttled | `partial:true`, `join_status:"incomplete"` if a joined side missing, warning set |
| **Error** | bad SQL / auth on a required connector | top-level `{error_code}`, HTTP 4xx/5xx, no envelope rows |

## Acceptance tests (gate)
- `test_rls_ast`: RLS policy → `assignee = <alice>` AND-ed into the Jira predicate set; the Jira mock is asserted to receive `assignee=<alice>` (forbidden rows never requested).
- `test_cls_mask`: `reporter_email` is `hash`-masked in output; `ColumnMeta.masked=True`; the raw email never appears in `rows`.
- `test_projection_union_guard`: a query ordering by a non-projected column → that column is added to `columns_to_fetch` for the source.
- `test_deny_overrides` *(unit, `EntitlementEngine`)*: a `deny` **and** a matching `allow` on the same
  resource → the resource lands in `denied_resources`; the allow does not win. Deny-overrides, asserted at
  the engine. (CORRECTION 3 — this previously said "→ empty", contradicting §Stage 3 and
  `test_entitlement_denied` below. The prose there matches the HLD §9 rail and wins.)
- `test_canonical_alice`: end-to-end canonical query as alice → the known non-empty joined rows; `freshness_ms` = the stalest side; `join_status="complete"`.
- `test_rls_shrinks_bob`: same query as bob → **exactly 1 row where alice got 3** (a count that shrinks but stays
  non-zero is the legible RLS proof; a count that collapses to zero is indistinguishable from a broken query);
  assert the Jira adapter received `predicates["assignee"] == "bob"`, so the forbidden rows were never fetched.
- `test_timeout_partial`: force the Jira mock to time out → `partial=true`, `join_status="incomplete"`, `reason=SOURCE_TIMEOUT`, GitHub rows present, no un-joined passthrough.
- `test_trichotomy`: one case each, asserting the three envelope shapes are distinct — **empty** (carol, who is
  assigned nothing: `rows=[]`, `partial=false`, `join_status="complete"`, HTTP 200), **partial** (Jira forced to
  time out), **error** (malformed SQL → HTTP 400, no envelope rows).
- `test_staleness_knob`: the same query twice — `max_staleness_ms=0` then `60000`. First returns
  `sources[].served == "live"` with `stats.connector_ms` populated; second returns `served == "cache"` with no
  connector time and **no token spent**. Assert `rate_limit_status[c].remaining` is **not decremented**
  (`>=` the first reading), not that it is byte-equal: the bucket refills against wall-clock time, so two
  requests straddling a refill interval legitimately differ. "Not decremented" is the observable form of
  "no token spent"; "unchanged" would be a time-dependent flake. *Why this exists at
  Phase 2:* freshness is 15% of the rubric, and its only other proof was the Phase-3 Playwright spec — which is
  SHOULD-tier and disappears if the UI is cut. This keeps the freshness claim backed by a MUST-tier test.
- `test_connector_gates`: a query naming a connector the tenant has not been granted → `403 CONNECTOR_NOT_ENABLED`;
  a granted connector whose seeded secret fails to decrypt → `403 CONNECTOR_AUTH_ERROR`. These are the two
  gateway codes in the six-code vocabulary and were the only ones with no test — a declared code with no test
  is a claim, not a feature. **Phase 1 raises 502 for this code** and must be corrected to 403 here
  (CORRECTION 8, ADR-029): HLD §4, design-doc §8.1 and this file all say 403, and HLD §9 makes the error
  vocabulary a provenance rail that must be identical across all three.
- `test_pagination`: a query with more joined rows than `LIMIT` returns a `next_cursor`; echoing it returns the
  next, **non-overlapping** window; the final window returns `next_cursor: null`. Needs at least two rows
  sharing the same `updated` value, so the run fails if the `key` tiebreaker is missing — **Phase 1's
  `mock_data.py` has twenty distinct timestamps and no ties**, so this phase gives `SUP-13` and `SUP-14`
  (two of alice's three In-Progress issues) the same `updated` value (CORRECTION 7, ADR-035). Changing a
  timestamp changes no row's membership in any persona's result, so alice 3 / bob 1 / carol 0 is preserved;
  it only makes the ordering non-total *within* alice's page, which is the exact condition the tiebreaker
  exists for.
- `test_entitlement_denied` *(route, end-to-end)*: a seeded `effect='deny'` policy on a referenced resource →
  `403 ENTITLEMENT_DENIED` (**not** an empty 200) — this is what keeps the code reachable and distinguishes
  explicit deny from default-deny. Paired with `test_deny_overrides` above: that one asserts the engine
  *decides* deny, this one asserts the decision *renders* as 403.
- `test_default_deny_is_empty`: a role with **no** matching allow → `rows: []`, HTTP **200**, `partial:false`.
  The other half of the split, and the one that keeps "empty" meaning *"ran fine, nothing matched"*.
- `test_cursor_null_when_partial`: force a timeout on a query whose result would otherwise page →
  `partial=true` and `next_cursor is None`.

## `make demo` — the artifact insurance *(build it here, at this gate)*
Both submission artifacts (console screenshot, trace waterfall) currently live in Phases 3 and 4, so a slip
leaves the submission with **no demo at all**. Close that here: `scripts/demo.sh` curls `/v1/query` four times
— alice, bob, `max_staleness_ms=0` then `60000`, and a forced Jira timeout — printing each envelope, and tees
the run to `docs/demo-output.txt`. That single file demonstrates four of the five hard parts with no UI and no
observability stack. Wire it to `make demo` (target declared in Phase 0).

## Done when
`POST /v1/query` runs the canonical query end-to-end through all five stages and every acceptance test is green. The envelope from this phase is what the UI (Phase 3) renders and what the trace (Phase 4) instruments.
