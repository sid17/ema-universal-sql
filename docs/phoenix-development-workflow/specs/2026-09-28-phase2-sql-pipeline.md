# Phase 2 — SQL Pipeline — Design Spec

> **Status:** ready for `/plan-phase`.
> **Source of truth:** `docs/design/phases/phase-2-sql-pipeline.md`
> — it wins on any conflict. Eight corrections were applied there first (see its header); this spec is written
> against the corrected file and adds the four things a phase file does not carry: **MUST/SHOULD/COULD tiering
> per task**, **explicit file paths**, **the test list as named files**, and **which README sections this phase
> fills**.
>
> **Conversion, not new design.** Every substrate decision is locked in `kickoff/v1` (ADR-002, 003, 008–012).
> The ten decisions Phase 2 genuinely opened are ADR-027…036 in `kickoff/v3/architecture.md`.

## Context

Phase 2 is the intellectual core and the phase the whole design doc rests on. Phases 0 and 1 built the
contracts and the sources; **nothing yet turns SQL into an answer** — `POST /v1/query` returns an empty
envelope and the connectors are reachable only from tests.

This phase makes two claims true in code, both from `02-DEFINITION-OF-DONE.md` §4:

1. **Entitlement is compiled into the plan and pushed down, never post-filtered.** The RLS predicate is
   AND-ed into the WHERE *before* the planner splits per source, so it rides pushdown to Jira and the
   forbidden rows are never requested. Measured in the v3 probe: the Jira fetch shrinks **9 → 3 → 1** by
   persona while GitHub stays 14 (correctly — the rule is on a Jira column).
2. **Pushdown is an optimization; the engine re-applies every predicate authoritatively** over a
   `projection ∪ WHERE ∪ ORDER BY ∪ join-key` fetch, so re-filtering cannot drop an entitled row.

Plus the correctness point that is graded and easy to lose: **empty ≠ partial ≠ error**.

## Architecture

```
POST /v1/query  (QueryRequest + UserContext, deadline from REQUEST_TIMEOUT_MS)
  │
  ├─ 2  SQLParser.parse_and_validate      src/sqlparse/
  │        parse(into=Select) → REJECT (node whitelist, raw AST) → qualify(schema)
  │        → ParsedQuery {ast, tables, projection, predicates, join_keys, order_by, limit}
  │        gate: every referenced connector granted+active, else 403 CONNECTOR_NOT_ENABLED
  │
  ├─ 3  EntitlementEngine.compile          src/entitlement/
  │        policies → RLS predicate AND-ed into WHERE · CLS mask rewrites the projection
  │        → EntitledPlan {parsed, masks, denied_resources, entitlement_scope}
  │
  ├─ 4  QueryPlanner.plan                  src/planner/
  │        flatten AND recursively (.unnest()) → group leaves by owning table
  │        capability check → pushable | residual · projection-union guard
  │        → QueryPlan {per-source {predicates_pushed, columns_to_fetch}, residual}
  │
  ├─ 5a FederationEngine.execute           src/execution/federation.py
  │        asyncio.gather(wait_for(adapter.fetch(), budget), return_exceptions=True)
  │        pa.Table(explicit schema) → duckdb :memory: register → residual SQL
  │        → FederationResult {rows, column_types, responses, failures}
  │
  ├─ 5b ResultAssembler.assemble           src/execution/assemble.py
  │        ColumnMeta(+masked) · freshness_ms=stalest · rate_limit_status
  │        · join_status/partial/warnings · result cursor → QueryEnvelope
  │
  └─    AuditLogger.write                  src/governance/audit.py
           one audit_logs row; query_text NORMALIZED (literals → ?)
```

**`QueryPipelineRunner`** (`src/pipeline/runner.py`) owns this order and nothing else. Each stage is
wrapped in `@stage_span` as it is written (03-BUILD-PROCESS's front-load-observability decision), so
Phase 4 renders a waterfall without re-reading five modules to find the boundaries.

### Files

| Path | New/Mod | What |
|---|---|---|
| `src/sqlparse/__init__.py` | new | package |
| `src/sqlparse/whitelist.py` | new | the measured node whitelist + `reject_unsupported()` |
| `src/sqlparse/parser.py` | new | `SQLParser`, `ParsedQuery`, `TableRef`, `ColumnRef`, `Predicate` |
| `src/entitlement/__init__.py` | new | package |
| `src/entitlement/predicate.py` | new | JSONB predicate AST → sqlglot node; `:user` binding |
| `src/entitlement/engine.py` | new | `EntitlementEngine`, `EntitledPlan`, RLS inject + CLS rewrite |
| `src/planner/__init__.py` | new | package |
| `src/planner/planner.py` | new | `QueryPlanner`, `QueryPlan`, `SourcePlan`, projection-union guard |
| `src/execution/__init__.py` | new | package |
| `src/execution/federation.py` | new | `FederationEngine`, `FederationResult`, deadline, DuckDB |
| `src/execution/assemble.py` | new | `ResultAssembler`, envelope + freshness + cursor |
| `src/pipeline/__init__.py` | new | package |
| `src/pipeline/runner.py` | new | `QueryPipelineRunner` — stage order, spans, stats |
| `src/pipeline/registry.py` | new | adapter construction/wiring from app.state (one place) |
| `src/governance/audit.py` | new | `AuditLogger` |
| `src/models/errors.py` | **mod** | `+ InvalidQueryError` (ADR-028) |
| `src/gateway/handlers.py` | **mod** | `+ InvalidQueryError` handler |
| `src/gateway/routes.py` | **mod** | `/v1/query` calls the runner; deadline actually used |
| `src/main.py` | **mod** | build the runner once in `lifespan`, hang on `app.state` |
| `src/governance/secrets.py` | **mod** | 502 → **403** (ADR-029) |
| `tests/unit/test_secrets.py` | **mod** | assert 403 |
| `src/connectors/mock_data.py` | **mod** | tie `SUP-13`/`SUP-14` on `updated` (ADR-035) |
| `config/policies.yaml` | **mod** | `+ deny` + `default-deny` fixtures for the two new tests |
| `scripts/demo.sh` | new | the four-call walkthrough |
| `Makefile` | **mod** | real `demo` target |
| `README.md` | **mod** | two sections (below) |

`src/models/` is otherwise **frozen** — Phase 0's contract is what Phases 3 and 4 consume.

## Tech stack + key decisions

| Decision | Pick | Why |
|---|---|---|
| Parse/rewrite | sqlglot 30.20, `read="duckdb"` | ADR-002 (locked) |
| Validation order | **reject → qualify** | ADR-027 — `qualify()` expands `SELECT *`, measured |
| Malformed SQL | `InvalidQueryError` / 400 `INVALID_QUERY` | ADR-028 — outside the six, on the `UnauthenticatedError` precedent |
| `CONNECTOR_AUTH_ERROR` | **403**, not 502 | ADR-029 — locked HLD §9 rail; Phase 1 drifted |
| Per-source split | manual recursive `.unnest()` flatten | ADR-002/Card 1 + the Phase 0 spike correction |
| Join engine | DuckDB `:memory:` per request | ADR-003 (locked) |
| Arrow schema | explicit, from capabilities | ADR-030 — `register()` raises on 0 columns, measured |
| deny semantics | explicit deny → 403; default-deny → empty | ADR-031 |
| Deadline | `wait_for` per source in `gather(return_exceptions=True)` | ADR-032 |
| Audit text | normalized, literals → `?` | ADR-033 |
| Stage 5 | two modules | ADR-034 (LAW 1/3) |
| Result cursor | reuse `connectors/pagination.py` codec, `strategy="result"` | ADR-036 (LAW 2) |
| Entitlement DI | `configure_entitlement(get_current_user)` partial | Card 4 front-half only |

## Data contracts

**The canonical query** (verbatim, HLD §4 = design-doc §6.1 — a provenance rail):

```sql
SELECT pr.title, pr.author, issue.key, issue.status
FROM   github.pull_requests pr
JOIN   jira.issues issue ON pr.issue_key = issue.key
WHERE  pr.repo = 'ema/core' AND pr.state = 'open' AND issue.status = 'In Progress'
ORDER BY issue.updated DESC
LIMIT 50
```

**Qualified AST, measured** (`.venv/bin/python`, 2026-09-28) — `qualify` wraps every projection in an
`Alias` and stamps every `Column` with its owning alias:

```
tables:    catalog='' db='github' name='pull_requests' alias='pr'
           catalog='' db='jira'   name='issues'        alias='issue'
selects:   Alias output_name=title | author | key | status
nodes:     Alias And Column EQ From Identifier Join Limit Literal Order Ordered
           Select Table TableAlias Where
```

**The per-source split this produces** (alice, after RLS injection):

| Source | pushed predicates | `columns_to_fetch` (projection ∪ WHERE ∪ ORDER ∪ join key) |
|---|---|---|
| `github.pull_requests` | `repo='ema/core'`, `state='open'` | `title, author, repo, state, issue_key` |
| `jira.issues` | `status='In Progress'`, **`assignee='alice'`** | `key, status, assignee, updated` |
| residual (in-engine) | the join `pr.issue_key = issue.key`, `ORDER BY`, `LIMIT` | — |

**The persona contract, measured end-to-end through the real pipeline:**

| Persona | GitHub fetched | Jira fetched | Joined |
|---|---|---|---|
| *(no RLS)* | 14 | 9 | 8 |
| alice | 14 | **3** | **3** |
| bob | 14 | **1** | **1** |
| carol | 14 | **1** | **0** |

The middle column is the proof that matters: it shrinks with the persona, so entitlement reached the
source. A post-filtering implementation would read 9 on every row of that table.

**Policy row → sqlglot node.** `{"op":"eq","col":"assignee","value":":user"}` with
`UserContext.user_id="alice"` compiles to `exp.EQ(this=exp.column("assignee","issue"),
expression=exp.Literal.string("alice"))`. The literal is what the *plan* carries; `assignee = currentUser()`
is only how a live Jira adapter would render it into JQL. **Assert at the adapter's received-predicates
layer** (`predicates["assignee"] == "alice"`), never against a JQL string.

## User flows

1. **alice runs the canonical query** → 200, 3 rows, `join_status:"complete"`, `partial:false`,
   `sources[]` both `ok`, `freshness_ms` = age of the **stalest** contributor, `trace_id` resolvable.
2. **bob runs the identical SQL** → 200, **1 row**. Same query text, different plan, and the Jira adapter
   received `assignee='bob'`. This is hard part 1.
3. **CLS preset** (canonical + `issue.reporter_email`) → `reporter_email` is an MD5 digest,
   `ColumnMeta.masked=true`, and no raw address appears anywhere in `rows`.
4. **Staleness knob** — same query twice, `max_staleness_ms` 0 then 60000 → `served` flips `live` → `cache`,
   `stats.connector_ms` drops out, `rate_limit_status[].remaining` is **not decremented**.
5. **Jira forced to time out** → 200, `partial:true`, `join_status:"incomplete"`, warning `SOURCE_TIMEOUT`,
   GitHub rows present, `next_cursor: null`, and the un-joined rows are *never* passed off as joined.
6. **carol** → 200, `rows:[]`, `partial:false`, `join_status:"complete"`. **Empty is an answer.**
7. **Malformed SQL** → 400 `INVALID_QUERY`, no envelope rows. Three shapes, three tests.

## Features by task, with tier

Tiers from `02-DEFINITION-OF-DONE.md` §3.

| # | Task | Tier | Notes |
|---|---|---|---|
| T101 | `mock_data` tie + `policies.yaml` deny fixtures | MUST | unblocks two tests that otherwise cannot fail |
| T102 | `InvalidQueryError` + handler; `secrets.py` 502→403 | MUST | ADR-028, ADR-029 |
| T103 | `whitelist.py` — measured node set | MUST | SQL subset (brief 19) |
| T104 | `parser.py` — parse → reject → qualify → extract | MUST | ADR-027 |
| T105 | connector gate in the parser | MUST | `CONNECTOR_NOT_ENABLED` |
| T106 | `predicate.py` — JSONB AST → sqlglot, `:user` | MUST | ADR-008 |
| T107 | `engine.py` — RLS inject | MUST | **non-negotiable #1** |
| T108 | `engine.py` — CLS rewrite + masks map | MUST | 1 column mask (brief 154) |
| T109 | `engine.py` — deny-overrides / default-deny | MUST | ADR-031 |
| T110 | `planner.py` — recursive flatten + per-source split | MUST | spike-corrected |
| T111 | `planner.py` — capability check, pushable vs residual | MUST | **non-negotiable #2** |
| T112 | `planner.py` — projection-union guard | MUST | **non-negotiable #2** |
| T113 | `federation.py` — parallel fetch + deadline | MUST | ADR-032; brief 24/84 |
| T114 | `federation.py` — Arrow + DuckDB execute | MUST | ADR-030 |
| T115 | `assemble.py` — envelope, columns, freshness | MUST | brief 152–153 |
| T116 | `assemble.py` — join_status / partial / warnings | MUST | **non-negotiable #3** |
| T117 | `assemble.py` — result cursor | SHOULD | DoD §3 SHOULD; reuses Phase 1 codec |
| T118 | `audit.py` — one row, normalized text | SHOULD | DoD §3 SHOULD; ADR-033 |
| T119 | `runner.py` + `registry.py` — stage order + spans | MUST | front-loaded observability |
| T120 | wire `/v1/query`; `main.py` lifespan | MUST | the phase's whole point |
| T121 | `scripts/demo.sh` + `make demo` | SHOULD | **artifact insurance** — carries the demo with no UI |
| T122 | README: two sections | MUST | submission gate 6 |

Nothing here is COULD-tier. The COULD list stays opportunistic and untouched.

## Test list (named files, so the plan can make them checkboxes)

**Unit — `tests/unit/`, infra-free (the commit hook runs this suite):**

| File | Covers |
|---|---|
| `test_whitelist.py` | each rejected construct by name; canonical query accepted |
| `test_parser.py` | parse/qualify/extract; `SELECT *` rejected; unknown column → 400; unknown table → gate |
| `test_predicate_ast.py` | JSONB AST → sqlglot; `:user` binding; unknown op raises |
| `test_entitlement.py` | `test_rls_ast`, `test_cls_mask`, `test_deny_overrides`, join-key mask ordering |
| `test_planner.py` | `test_projection_union_guard`, pushable vs residual, required-column absent |
| `test_federation.py` | deadline → timeout outcome; `return_exceptions` keeps the sibling's rows; empty-schema register |
| `test_assemble.py` | freshness = stalest; join_status matrix; cursor null when partial |
| `test_audit.py` | `query_text` normalized — a literal email never reaches the row |
| `test_runner.py` | stage order; spans emitted per stage |

**Integration — `tests/integration/`, needs `make up && make seed`:**

| File | Covers (phase-file gate names in bold) |
|---|---|
| `test_canonical.py` | **`test_canonical_alice`**, **`test_rls_shrinks_bob`** |
| `test_cls.py` | **`test_cls_mask`** end-to-end; raw email absent from the response body |
| `test_trichotomy.py` | **`test_trichotomy`** — empty (carol) / partial / error, three shapes |
| `test_timeout_partial.py` | **`test_timeout_partial`**, **`test_cursor_null_when_partial`** |
| `test_staleness_knob.py` | **`test_staleness_knob`** — live→cache, remaining not decremented |
| `test_connector_gates.py` | **`test_connector_gates`** — both gateway codes, both 403 |
| `test_entitlement_denied.py` | **`test_entitlement_denied`** + `test_default_deny_is_empty` |
| `test_pagination.py` | **`test_pagination`** — non-overlapping windows, tiebreaker load-bearing |

Three DoD §2 rows name integration paths directly — `test_staleness_knob.py`, `test_timeout_partial.py`,
`test_trichotomy.py` — so those filenames are a contract, not a preference.

## README sections this phase fills

From Phase 0's README-growth table:

- **What it proves — the five hard parts, each with its command.** Fillable now because `make demo` exists.
- **Trade-offs + join strategy (federated vs materialized).** Brief line 69 asks for this explicitly; it is
  a MUST-tier row in DoD §3 that no other phase owns.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| A silent post-filter creeps in under time pressure and the design doc's central claim becomes false | The test asserts the **adapter's received predicates**, not the row count. A post-filtering build passes a row-count test and fails this one. |
| `qualify()` accepts an unknown table, so a typo'd connector reaches the planner | Explicit `find_all(exp.Table)` gate in the parser (measured necessity, v3 Finding 2) |
| A zero-row source crashes `duckdb.register()` in the demo | Explicit Arrow schema from capabilities, always (ADR-030) |
| `gather` cancels the healthy source when one times out, collapsing partial→error | `return_exceptions=True`, asserted by a named unit test |
| `federation.py` grows past 500 lines and the commit hook blocks | Split at ADR-034's seam **before** writing, not after |
| The audit row leaks the PII that CLS strips | Normalized `query_text` (ADR-033), asserted by `test_audit.py` |
| Result and connector cursors get confused | Strategy tag in the token; a connector cursor presented as a result cursor raises |

## Source artifacts

- Phase file: `docs/design/phases/phase-2-sql-pipeline.md` (8 corrections applied at source)
- ADRs: `docs/kickoff/v3/architecture.md` (027–036); `docs/kickoff/v1/architecture.md` (002, 003, 008–012)
- Research/probe: `docs/kickoff/v3/research-repos.md`
- Gate: `docs/design/02-DEFINITION-OF-DONE.md` §2 hard parts + §4 non-negotiables
- Rails: `docs/design/00-PROTOTYPE-HLD.md` §4, §5, §9
