# Research — Phase 2 (SQL pipeline)

> **Scope of this pass.** `03-BUILD-PROCESS.md` step 1 caps per-phase research at a 10–20 minute
> confirmation and forbids it from reopening a locked decision. Phase 2's prior art was already
> deep-read in `01-EXECUTION-PLAN.md` §D — Card 1 (`sqlglot`), Card 3 (`universql`) and Card 4
> (`fastapi-permissions`) are *this phase's* cards. **No new repos were surveyed.** There was no open
> library question: every substrate is locked and every pattern was extracted in v1.

What Phase 2 actually needed was not a survey but a **runtime probe**. The Phase 0 spike (14/14) proved
parse → qualify → RLS → split → DuckDB on a *hand-written* fixture. It never ran against the real
capability models, the real mock data, or the failure shapes Phase 2 must handle. `/plan-phase`'s
"Step 0: inspect real data" is the applicable rule, so that is what this pass did.

Five findings below. Three of them change the design; one of them would have been a crash.

---

## Finding 1 — `qualify()` **expands** `SELECT *`, so the whitelist cannot run after it

The phase file says *"Qualify FIRST (mandatory)"* and, separately, that the node whitelist must
*"catch `exp.Star` (reject `SELECT *`)"*. Measured, those two instructions are incompatible:

```
before qualify: ['From', 'Identifier', 'Select', 'Star', 'Table', 'TableAlias']
after  qualify: SELECT "issue"."key" AS "key", "issue"."status" AS "status",
                       "issue"."assignee" AS "assignee", "issue"."reporter_email" AS "reporter_email",
                       "issue"."project" AS "project", "issue"."updated" AS "updated"
                FROM "jira"."issues" AS "issue"
```

`exp.Star` is **gone** by the time a post-qualify validator runs. A whitelist placed there would accept
`SELECT *` silently — and `SELECT *` is the one projection that defeats projection pushdown, because the
fetch set becomes every column the source has.

→ **ADR-027.** Reject first, qualify second. "Qualify first" is a true statement about *column
attribution*, which only stages 3–5 need; it is not a statement about validation order.

## Finding 2 — `qualify()` rejects an unknown **column** but accepts an unknown **table**

```
SELECT issue.nope FROM jira.issues issue   -> OptimizeError: Unknown column: nope
SELECT s.a       FROM slack.msgs s         -> ACCEPTED, no error
```

So `validate_qualify_columns` really is free column validation (Card 1 was right), but it is **not** a
connector gate. A query naming `slack.msgs` sails through the optimizer and would only fail later, deep
in the planner, with an unhelpful message.

→ The `CONNECTOR_NOT_ENABLED` gate must be an explicit `find_all(exp.Table)` check on `(db, name)`
against the tenant's grants. The phase file already requires this; the probe upgrades it from
"belt and braces" to "the only thing standing there".

## Finding 3 — `duckdb.register()` **raises** on a zero-column Arrow table

This is the one that would have been a crash in the demo.

```python
pa.Table.from_pylist([])          # -> 0 columns
con.register("t", empty)          # -> InvalidInputException:
                                  #    Provided table/dataframe must have at least one column
```

`pa.Table.from_pylist(rows)` infers its schema **from the rows**. An empty row list therefore infers
*nothing*, and DuckDB refuses to register it. Every zero-row path in Phase 2 hits this: a denied
resource, an over-restrictive RLS binding, a timed-out source contributing no rows to a partial result.
carol's persona — the `empty` leg of the trichotomy — is one join away from it.

With an explicit schema it is fine:

```python
pa.Table.from_pylist([], schema=pa.schema([("key", pa.string()), ("status", pa.string())]))
con.register("t", e2)             # ok, 0 rows
```

→ **ADR-030.** The Arrow schema is built from the **capability model's columns**, never inferred from
the rows. Inference is only safe when there is at least one row, which is exactly the case we cannot
guarantee.

## Finding 4 — the canonical node set, measured rather than guessed

The phase file's whitelist sketch (`{Select, From, Join, Where, Order, Limit, Column, Table, Literal,
EQ/NEQ/GT/…, And/Or, Alias}`) is missing three node types that the canonical query actually contains:

```
canonical (qualified): Alias, And, Column, EQ, From, Identifier, Join, Limit,
                       Literal, Order, Ordered, Select, Table, TableAlias, Where
```

`Identifier`, `TableAlias` and `Ordered` are structural nodes sqlglot always emits — omitting them from
the whitelist would reject the canonical query itself. Conversely the probe confirms the node types
that *must* stay out, each with the construct that produces it:

| Construct | Node emitted |
|---|---|
| `WHERE x IN (SELECT …)` | `In`, `Subquery` |
| `COUNT(x)` | `Count` (an `exp.Func` subclass) |
| `… UNION …` | `Union` |
| `WHERE x LIKE 'a%'` | `Like` |
| `GROUP BY x` | `Group` |

→ The whitelist is built from this measured set, and the test asserts each rejected construct by name.

## Finding 5 — the persona contract survives the real pipeline, and RLS really does push down

Running the canonical query against `src/connectors/mock_data.py` (not the spike's fixture) through
parse → qualify → RLS inject → per-source split → DuckDB join:

| Persona | GitHub rows fetched | Jira rows fetched | Joined result |
|---|---|---|---|
| *(no RLS)* | 14 | 9 | **8** |
| `alice` | 14 | **3** | **3** |
| `bob` | 14 | **1** | **1** |
| `carol` | 14 | **1** | **0** |

Two things are proven here that a row count alone would not show:

- **The Jira fetch shrinks with the persona (9 → 3 → 1).** The forbidden rows are never requested, which
  is non-negotiable #1. If entitlement were post-filtered, that column would read 9 every time.
- **carol fetches 1 Jira row and joins to 0.** Her emptiness comes from the *join* (her only PR is
  closed), not from having no data — which is what makes her a real `empty` case rather than a
  vacuous one.

The GitHub column stays 14 for every persona, correctly: the RLS rule is on `jira.issues.assignee`, and
GitHub has no such column.

---

## What was NOT reopened

Locked and untouched by this pass, per the hard rule: sqlglot, DuckDB `:memory:`, pyarrow as the
internal currency, manual per-source split (never `pushdown_predicates`), `(db, name)` table mapping,
JSONB predicate AST, the equijoin, the canonical RLS/CLS pair, and the `configure_permissions`-shaped
DI factory (front-half only).

## Next step
→ Phase 2 architecture: `docs/kickoff/v3/architecture.md` (ADR-027…036).
