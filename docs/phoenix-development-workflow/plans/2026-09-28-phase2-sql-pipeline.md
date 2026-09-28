# Plan — Phase 2: SQL Pipeline

**Goal:** `POST /v1/query` runs the canonical query end-to-end through all five stages, with entitlement
compiled into the plan (never post-filtered), honest degradation, and the three outcomes kept distinct.

**Depends on:** Phase 0 (contracts, gateway, control plane, observability seams) · Phase 1 (adapters,
token bucket, freshness cache, secrets, seeded control plane).

**Assumes:**
- `src/models/` is frozen except for adding `InvalidQueryError` (ADR-028).
- Phase 1's adapters are called *only* through `BaseConnectorAdapter.fetch()`; this phase adds no second seam.
- `pyproject.toml` already declares sqlglot / duckdb / pyarrow — **no task edits it**.
- `.venv/bin/python` (`python` is not on PATH).

**Verify (the Iron Law — run it, read the output, then claim it):**
```
.venv/bin/python -m pytest -q tests/unit           # hermetic, no containers
make up && make seed && make test-integration      # the real stack
make demo                                          # writes docs/demo-output.txt
.venv/bin/ruff check src tests                     # lint; see the gate note on `format`
wc -l src/**/*.py | sort -n | tail -5              # every file < 400
```

**Spec:** `../specs/2026-09-28-phase2-sql-pipeline.md` · **Phase file wins on conflict:**
`../../design/phases/phase-2-sql-pipeline.md` (8 corrections already applied at its head) ·
**ADRs:** `../../kickoff/v3/architecture.md` (027–036).

---

## Step 0 — inspect real data  ✅ DONE

Run before planning, not during build. Results are in `docs/kickoff/v3/research-repos.md` and are the
reason four tasks below exist at all:

- `qualify()` **expands** `SELECT *` → the whitelist must run first (T103/T104).
- `qualify()` rejects an unknown *column* but **accepts an unknown table** → explicit gate (T105).
- `duckdb.register()` **raises** on a zero-column Arrow table → explicit schema (T114).
- `mock_data.py` has **no tied `updated` values** → `test_pagination` could not fail (T101).
- Persona contract holds through the real pipeline: alice 3 / bob 1 / carol 0, Jira fetch 9→3→1.

## File map

**Create:** `src/sqlparse/{__init__,whitelist,parser}.py` · `src/entitlement/{__init__,predicate,engine}.py` ·
`src/planner/{__init__,planner}.py` · `src/execution/{__init__,federation,assemble}.py` ·
`src/pipeline/{__init__,runner,registry}.py` · `src/governance/audit.py` · `scripts/demo.sh` ·
`tests/unit/{test_whitelist,test_parser,test_predicate_ast,test_entitlement,test_planner,test_federation,test_assemble,test_audit,test_runner}.py` ·
`tests/integration/{test_canonical,test_cls,test_trichotomy,test_timeout_partial,test_staleness_knob,test_connector_gates,test_entitlement_denied,test_pagination}.py`

**Modify:** `src/models/errors.py` · `src/gateway/handlers.py` · `src/gateway/routes.py` · `src/main.py` ·
`src/governance/secrets.py` · `src/connectors/mock_data.py` · `tests/unit/test_secrets.py` ·
`config/policies.yaml` · `scripts/seed.py` (if the deny fixtures need it) · `Makefile` · `README.md` ·
`docs/design/03-BUILD-PROCESS.md` (tracker)

**Delete:** `tests/unit/ast_techniques.py`, `tests/unit/ast_fixtures.py`, `tests/unit/test_ast_spike.py` —
**at T119, not before.** `ast_techniques.py`'s own docstring says so: *"Phase 2 reimplements each technique
in `src/sqlparse`, `src/entitlement`, `src/planner` and `src/execution`; at that point these assertions
should be rewritten against those modules and this file deleted."* Deleting earlier loses the only
green reference while the replacements are half-written.

---

## Milestone A — foundational (BLOCKING; nothing else starts until these are green)

- [x] **T101 — Fixtures that let two tests fail**
  Files: `src/connectors/mock_data.py`, `config/policies.yaml`
  **Decision:** tie `SUP-13` and `SUP-14` on `updated` rather than adding rows, because changing a timestamp
  changes no row's *membership* in any persona result — alice 3 / bob 1 / carol 0 is preserved — while adding
  a row would move those counts, and they are the demo (ADR-035).
  **Decision:** the deny fixtures go in `policies.yaml` as two new policies scoped to roles **no persona
  holds by default** (`auditor` for the explicit deny, and a role with no allow at all for default-deny), so
  the canonical demo is untouched and the new tests mint their own token.
  Also update `tests/unit/test_mock_data.py` with a test asserting the tie *exists* — the tie is now load-bearing.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_mock_data.py`

- [x] **T102 — `INVALID_QUERY` + the `CONNECTOR_AUTH_ERROR` correction**
  Files: `src/models/errors.py`, `src/gateway/handlers.py`, `src/governance/secrets.py`,
  `tests/unit/test_errors.py`, `tests/unit/test_secrets.py`
  `InvalidQueryError(message, detail=None)` → `error_code="INVALID_QUERY"`, `http=400`; handler registered
  alongside the existing two. `secrets.py` 502 → **403** (ADR-029) and its four tests updated.
  **Decision:** a new exception type, not a seventh `ErrorCode` — `models/errors.py` states in its own
  docstring that a seventh means changing the submitted design doc, and `UnauthenticatedError` already sets
  the precedent for a request-shape failure living outside the six (ADR-028).
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_errors.py tests/unit/test_secrets.py`

## Milestone B — Stage 2, the parser  (P1)

- [x] **T103 — `whitelist.py`: the measured node set**
  Files: `src/sqlparse/{__init__,whitelist}.py`, `tests/unit/test_whitelist.py`
  `ALLOWED_NODES` built from the *measured* canonical set (incl. `Identifier`, `TableAlias`, `Ordered`,
  which the phase file's sketch omitted), plus the comparison/boolean operators. `reject_unsupported(tree)`
  walks and raises `InvalidQueryError` naming the offending node type.
  Test each rejected construct **by name**: `SELECT *`→`Star`, `IN (SELECT…)`→`Subquery`, `COUNT()`→`Count`,
  `UNION`→`Union`, `LIKE`→`Like`, `GROUP BY`→`Group`; and the canonical query accepted.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_whitelist.py`

- [x] **T104 — `parser.py`: parse → reject → qualify → extract**
  Files: `src/sqlparse/parser.py`, `tests/unit/test_parser.py`
  `ParsedQuery` dataclass per the phase file. `parse_one(..., into=exp.Select)` (free DDL/DML rejection),
  then `reject_unsupported` on the **raw** tree, then `qualify(schema=…)`. Catch `ParseError` and
  `OptimizeError` → `InvalidQueryError` 400. Map tables on **`(db, name)`**, keep `.alias`.
  **Decision:** the schema handed to `qualify` is built from `control_plane.get_capabilities()`, not a
  literal — the adapter and the parser must not be able to disagree about what columns exist.
  Comment the ordering: whitelist before qualify (ADR-027), and whitelist on the user's AST before stage 3
  injects `MD5` (trusted engine nodes are never re-validated).
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_parser.py`

- [x] **T105 — the connector gate**
  Files: `src/sqlparse/parser.py`, `tests/unit/test_parser.py`
  Every `(db, name)` must resolve to a connector the tenant has granted and that is `enabled`+`active`,
  else `403 CONNECTOR_NOT_ENABLED` naming the connector. Load-bearing because `qualify()` silently accepts
  an unknown table (measured).
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_parser.py -k gate`

## Milestone C — Stage 3, entitlement (the crux)  (P1)

- [x] **T106 — `predicate.py`: JSONB AST → sqlglot**
  Files: `src/entitlement/{__init__,predicate}.py`, `tests/unit/test_predicate_ast.py`
  `compile_predicate(node, alias, bindings)` for `{op, col, value}` with `op ∈ {eq, ne, gt, gte, lt, lte, in}`
  and `and`/`or` nesting. `":user"` binds from `UserContext.user_id`.
  **Decision:** an unknown `op` or an unbound `:param` **raises** (LAW 4). A policy that silently compiles to
  nothing is a policy that silently grants everything — the one failure mode an entitlement engine may not have.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_predicate_ast.py`

- [x] **T107 — `engine.py`: RLS injected into the plan**  ← **non-negotiable #1**
  Files: `src/entitlement/engine.py`, `tests/unit/test_entitlement.py`
  `EntitledPlan` dataclass. Fetch policies once via `get_policies(tenant, connectors, resources, roles)`;
  compile each RLS predicate and AND it in with `tree.where(pred, append=True)`. `entitlement_scope` =
  `user_id` (ADR-025's Phase 2 filler).
  `test_rls_ast`: the Jira predicate set contains `assignee = 'alice'`; **no string concatenation anywhere**.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_entitlement.py -k rls`

- [x] **T108 — `engine.py`: CLS mask rewrite**
  Files: `src/entitlement/engine.py`, `tests/unit/test_entitlement.py`
  For each CLS policy rewrite the projection in place: `hash`→`alias_(func("MD5", col), name)`,
  `null`→`Null()`, `redact`→literal `'••••'`, `drop`→pop from `tree.selects` *and* from the fetch set.
  Register `masks["jira.issues.reporter_email"]="hash"`. `exp.alias_` keeps the output column name stable.
  **Decision:** masking happens once, in the projection AST, and is *executed* by DuckDB's final SELECT —
  which is post-join by construction, satisfying the join-key rule with no second code path.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_entitlement.py -k cls`

- [x] **T109 — `engine.py`: deny-overrides and default-deny**
  Files: `src/entitlement/engine.py`, `tests/unit/test_entitlement.py`
  Explicit `effect='deny'` on a referenced resource → `denied_resources`, rendered later as 403. No matching
  allow → the resource yields **empty**, not 403 and not open. Deny always beats a matching allow (ADR-031).
  `test_deny_overrides` is the **unit** half; the 403 rendering is asserted end-to-end in T120's tests.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_entitlement.py -k deny`

## Milestone D — Stage 4, the planner  (P1)

- [x] **T110 — recursive conjunction flatten + per-source split**
  Files: `src/planner/{__init__,planner}.py`, `tests/unit/test_planner.py`
  `_flatten_conjunction` recursing through `.unnest()` — **not** `where.this.flatten()`. Group each leaf by
  `{c.table for c in leaf.find_all(exp.Column)}`; single-owner → candidate, multi-owner (the join) → residual.
  The regression test must inject RLS **first** and then assert the original predicates still separate — that
  is the exact spike finding (`tree.where(append=True)` parenthesizes and `flatten()` prunes at the paren).
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_planner.py -k flatten`

- [x] **T111 — capability check: pushable vs residual**  ← **non-negotiable #2**
  Files: `src/planner/planner.py`, `tests/unit/test_planner.py`
  A candidate is pushable only if `capabilities.supports(column, op)`; otherwise it stays residual and the
  engine re-applies it. A missing **required** column (GitHub's `repo`) → `InvalidQueryError` 400 naming it.
  **Decision:** unsupported ⇒ residual, never dropped. A dropped predicate returns rows the caller did not
  ask for, and when that predicate is the RLS filter it is a data leak, not a bug.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_planner.py -k capability`

- [x] **T112 — projection-union guard**  ← **non-negotiable #2**
  Files: `src/planner/planner.py`, `tests/unit/test_planner.py`
  `columns_to_fetch = projection(for that source) ∪ every column in any WHERE / ORDER BY / join key on it`.
  Computed **after** stage 3, so the CLS-rewritten `MD5(issue.reporter_email)` still contributes its column.
  `test_projection_union_guard`: ordering by a non-projected column adds it to the fetch set.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_planner.py -k projection_union`

## Milestone E — Stage 5a, federation  (P1)

- [x] **T113 — parallel fetch with a real deadline**
  Files: `src/execution/{__init__,federation}.py`, `tests/unit/test_federation.py`
  `asyncio.gather(*[wait_for(adapter.fetch(req), budget) for …], return_exceptions=True)`; budget descends
  from `REQUEST_TIMEOUT_MS`. `TimeoutError` → `SourceOutcome(state="timeout")`; `ApiError` from a source →
  `state="error"`; both leave the sibling's rows intact.
  **Decision:** `return_exceptions=True` is mandatory — the default cancels the healthy fetch, turning a
  `partial` answer into an `error` one (ADR-032). A named test asserts the sibling survives.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_federation.py -k "timeout or sibling"`

- [x] **T114 — Arrow build + DuckDB execute**
  Files: `src/execution/federation.py`, `tests/unit/test_federation.py`
  Explicit `pa.schema(...)` from `columns_to_fetch` — **always**, not only when empty (ADR-030). Rebind
  `github.pull_requests AS pr` → `github_pull_requests AS pr` keeping the alias. `duckdb.connect(":memory:")`
  per request, `register()`, run the residual SQL, `to_arrow_table()` (**not** `.arrow()` — returns a
  `RecordBatchReader` on 1.5.x). Read column types from `cursor.description`.
  A named test registers a **zero-row** source and asserts it does not raise.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_federation.py`

## Milestone F — Stage 5b, assembly  (P1)

- [x] **T115 — envelope, columns, freshness**
  Files: `src/execution/assemble.py`, `tests/unit/test_assemble.py`
  `ColumnMeta{name,type,source,masked}` (`masked` from `EntitledPlan.masks`); `freshness_ms =
  int((now − min(fetched_at)) * 1000)` — the **stalest** contributor; `rate_limit_status` from
  `limiter.remaining()`; `sources[]` from the `AdapterResponse`s. Exceeding `max_staleness_ms` with no
  refresh possible → a `STALE_DATA` warning.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_assemble.py -k "freshness or columns"`

- [x] **T116 — join_status / partial / warnings**  ← **non-negotiable #3**
  Files: `src/execution/assemble.py`, `tests/unit/test_assemble.py`
  A joined side missing → `partial=True`, `join_status="incomplete"`, warning `{code:SOURCE_TIMEOUT, connector}`,
  return the driving side's rows and **never** pass the un-joined set off as joined. Non-join multi-source
  scan with one side missing → `partial=True`, `join_status="n/a"`. All three outcomes asserted as distinct shapes.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_assemble.py -k "join_status or partial"`

- [x] **T117 — result-level cursor**  *(SHOULD)*
  Files: `src/execution/assemble.py`, `tests/unit/test_assemble.py`
  Reuse `connectors/pagination.py`'s `encode_token`/`decode_token` with `strategy="result"` (LAW 2, ADR-036).
  Offset over the **sorted entitled** rows; `null` when exhausted; `null` whenever `partial=True`.
  Sort is `updated DESC, key ASC` — the tiebreaker is why T101 exists.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_assemble.py -k cursor`

- [x] **T118 — `AuditLogger`**  *(SHOULD)*
  Files: `src/governance/audit.py`, `tests/unit/test_audit.py`
  One `audit_logs` row per query. `query_text` is the **normalized** SQL (literals → `?`) produced from the
  AST, never the raw string (ADR-033).
  **Decision:** normalize from the AST rather than by regex — a regex over SQL is exactly the string-level
  reasoning this whole design replaced with an AST, and it would miss quoted forms.
  The test asserts a literal email in the SQL **does not appear** in the stored row.
  Verify: `.venv/bin/python -m pytest -q tests/unit/test_audit.py`

## Milestone G — wiring, demo, docs

- [x] **T119 — `runner.py` + `registry.py`, and retire the spike**
  Files: `src/pipeline/{__init__,runner,registry}.py`, `tests/unit/test_runner.py`;
  delete `tests/unit/{ast_techniques,ast_fixtures,test_ast_spike}.py`
  `QueryPipelineRunner.run(request, user)` owns the five-stage order and nothing else; each stage wrapped in
  `@stage_span` with its ms into `envelope.stats`. `registry.py` constructs the two adapters from `app.state`
  in **one** place, so no module builds its own cache/limiter/secrets trio.
  Delete the spike **now**, per its own docstring — its techniques are reimplemented in `src/` and asserted
  by T103–T114.
  Verify: `.venv/bin/python -m pytest -q tests/unit` (whole suite green after the deletion)

- [x] **T120 — wire `/v1/query` and the integration suite**
  Files: `src/gateway/routes.py`, `src/main.py`, all eight `tests/integration/test_*.py`
  Runner built once in `lifespan`, hung on `app.state`; the route resolves it, passes the deadline, returns
  the envelope. The route handler stays under 80 lines (LAW 1) — it resolves, calls, returns.
  Verify: `make up && make seed && make test-integration`

- [x] **T121 — `make demo`**  *(SHOULD — artifact insurance)*
  Files: `scripts/demo.sh`, `Makefile`
  Four `curl`s — alice, bob, `max_staleness_ms` 0 then 60000, and a forced Jira timeout — printing each
  envelope and teeing to `docs/demo-output.txt`. Four of the five hard parts, with no UI and no
  observability stack. **Decision:** `bash` + `curl` + `python -m json.tool`, no new dependency — the
  reviewer runs this on a fresh clone.
  Verify: `make demo && test -s docs/demo-output.txt`

- [x] **T122 — README + tracker**
  Files: `README.md`, `docs/design/03-BUILD-PROCESS.md`
  Fill **"What it proves"** (five hard parts, each with its command) and **"Trade-offs + join strategy"**
  (federated vs materialized — brief line 69, a MUST no other phase owns). Tick the Phase 1 and Phase 2
  rows in the tracker.
  Verify: read it end-to-end once, out loud (DoD §1 gate 6)

---

## Verification gate

The phase is **not done** until all of these pass, regardless of checkbox state.

| # | Command | Expected |
|---|---|---|
| 1 | `.venv/bin/python -m pytest -q tests/unit` | all green, **0 containers** |
| 2 | `make up && make seed && make test-integration` | all green incl. the 8 new files |
| 3 | `make demo` | 4 envelopes; `docs/demo-output.txt` non-empty |
| 4 | alice vs bob in the demo output | **3 rows → 1 row**, non-zero |
| 5 | grep the demo output for `@acme.com` | **no match** in any CLS-preset `rows` |
| 6 | `.venv/bin/ruff check src tests` | clean, **no ignore list, no new `noqa`** |

> **Gate note on `ruff format`.** The repo's standard is `ruff check` (lint), which Phases 0 and 1 both
> held. `ruff format --check` was **never** part of any gate and **13 files at `8795197` do not satisfy
> it** — including `mock_data.py`, whose one-row-per-line tuple layout the file's own docstring calls
> deliberate and which `ruff format` explodes from 167 to 341 lines. Phase 2 does not reformat the tree:
> a 13-file formatting diff in the middle of the pipeline phase would bury the change under noise. Raised
> as a standing item instead — see the handoff.
| 7 | `wc -l` every new `src/` file | all **< 400** |
| 8 | the three envelope shapes | empty / partial / error visibly distinct |

**End-to-end flow test (not just per-task verification):** a single run that does
`mint(alice) → query → mint(bob) → same SQL → compare row counts → force timeout → assert partial →
staleness 0 then 60000 → assert live→cache`. The acceptance test is *"does the flow work?"*, not
*"do the parts work in isolation?"*

## Self-review checklist

- [x] Every spec requirement traced to ≥1 task (T101–T122 ↔ spec §Features by task)
- [x] No placeholder text
- [x] Every task has Files + a verify command
- [x] Foundational tasks (T101, T102) block the rest and are not parallelized
- [x] Tests live in the same task as the code they cover (LAW 6)
- [x] No task produces a file over ~400 lines — stage 5 is pre-split at ADR-034's seam
- [x] Every task involving a choice carries a **Decision** line
