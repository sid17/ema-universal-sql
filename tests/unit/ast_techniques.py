"""The sqlglot/DuckDB techniques Phase 2 is built on, extracted from the Phase 0
spike (`spike/ast_spike.py`, 14/14 green) so `test_ast_spike.py` can assert on
them without either file breaching LAW 1's 400-line limit.

This is deliberately NOT production code — nothing in `src/` imports it. Phase 2
reimplements each technique in `src/sqlparse`, `src/entitlement`, `src/planner`
and `src/execution`; at that point these assertions should be rewritten against
those modules and this file deleted.
"""

from __future__ import annotations

import duckdb
import pyarrow as pa
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

from tests.unit.ast_fixtures import (
    DATASETS,
    REGISTERED,
    SCHEMA,
)

# --------------------------------------------------------------------------
# Locked inputs (HLD §4 / §9 — do not drift)
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Step 1 — parse + qualify
# --------------------------------------------------------------------------


def parse_and_qualify(sql: str) -> exp.Select:
    """Card 1 finding #1: qualify() MUST run first — it stamps every exp.Column
    with its owning table/alias. Without it .table is empty and a predicate
    cannot be attributed to a source."""
    tree = sqlglot.parse_one(sql, read="duckdb")
    return qualify(tree, schema=SCHEMA, dialect="duckdb")


def alias_to_source(tree: exp.Select) -> dict[str, tuple[str, str]]:
    """Card 1 gotcha: `github.pull_requests` parses as .db='github',
    .name='pull_requests', .catalog='' — map on (db, name), not catalog."""
    out: dict[str, tuple[str, str]] = {}
    for table in tree.find_all(exp.Table):
        out[table.alias_or_name] = (table.db, table.name)
    return out


# --------------------------------------------------------------------------
# Step 2 — RLS injection (no string concatenation)
# --------------------------------------------------------------------------


def inject_rls(tree: exp.Select, alias: str, column: str, user: str) -> exp.Select:
    """Canonical RLS (HLD §9): jira.issues.assignee = :user, AND-ed into WHERE."""
    predicate = exp.EQ(
        this=exp.column(column, alias),
        expression=exp.Literal.string(user),
    )
    return tree.where(predicate, append=True)


# --------------------------------------------------------------------------
# Step 3 — CLS mask rewrite
# --------------------------------------------------------------------------


def apply_cls_mask(tree: exp.Select, alias: str, column: str) -> bool:
    """Canonical CLS (HLD §9): mask jira.issues.reporter_email, kind=hash.
    Wrapping in alias_ keeps the output column name stable."""
    for projection in tree.selects:
        if projection.output_name != column:
            continue
        projection.replace(exp.alias_(exp.func("MD5", exp.column(column, alias)), column))
        return True
    return False


# --------------------------------------------------------------------------
# Step 4 — per-source predicate split + projection-union guard
# --------------------------------------------------------------------------


def _flatten_conjunction(node: exp.Expression):
    """Yield the leaf conjuncts of an AND tree.

    SPIKE FINDING (corrects Card 1): a single `where.this.flatten()` is NOT
    enough. `tree.where(pred, append=True)` routes through `exp.and_`, which
    wraps the *existing* WHERE in an `exp.Paren` before AND-ing. `flatten()`
    prunes at the paren and yields the whole nested AND as one leaf, so the
    original three predicates never separate and nothing is pushable. Recurse
    through `unnest()` instead.
    """
    node = node.unnest()
    if isinstance(node, exp.And):
        yield from _flatten_conjunction(node.this)
        yield from _flatten_conjunction(node.expression)
    else:
        yield node


def where_leaves(tree: exp.Select) -> list[exp.Expression]:
    """Card 1 finding #2: do NOT reuse sqlglot's pushdown_predicates pass — it
    pushes within one AST, not out to independent connectors. Flatten manually."""
    where = tree.args.get("where")
    if where is None:
        return []
    return list(_flatten_conjunction(where.this))


def split_predicates(
    tree: exp.Select, aliases: dict[str, tuple[str, str]]
) -> tuple[dict[str, list[str]], list[str]]:
    """Group each leaf predicate by its owning table. A leaf touching 2+ tables
    is not pushable — which is exactly the guard we want."""
    pushable: dict[str, list[str]] = {alias: [] for alias in aliases}
    residual: list[str] = []
    for leaf in where_leaves(tree):
        owners = {c.table for c in leaf.find_all(exp.Column)}
        if len(owners) == 1 and (owner := owners.pop()) in pushable:
            pushable[owner].append(leaf.sql(dialect="duckdb"))
        else:
            residual.append(leaf.sql(dialect="duckdb"))
    return pushable, residual


def projection_union(tree: exp.Select, aliases: dict[str, tuple[str, str]]) -> dict[str, set[str]]:
    """Non-negotiable #2 (DoD §4): fetch = projection UNION every WHERE/ORDER BY
    /JOIN-key column, so the engine's authoritative re-filter cannot drop a row
    we were entitled to."""
    needed: dict[str, set[str]] = {alias: set() for alias in aliases}
    for column in tree.find_all(exp.Column):
        if column.table in needed:
            needed[column.table].add(column.name)
    return needed


# --------------------------------------------------------------------------
# Fetch simulation — proves the pushed predicates really filter at the source
# --------------------------------------------------------------------------


def evaluate_pushed(rows: list[dict], predicates: list[exp.Expression]) -> list[dict]:
    """Stand-in for a connector applying the pushed-down filters. Only the EQ
    form the canonical query needs; Phase 1's adapters do this for real."""
    out = rows
    for predicate in predicates:
        if not isinstance(predicate, exp.EQ):
            raise AssertionError(f"spike only pushes EQ, got {type(predicate).__name__}")
        column = predicate.this.name
        value = predicate.expression.this
        out = [row for row in out if row.get(column) == value]
    return out


def fetch(tree: exp.Select, aliases: dict[str, tuple[str, str]]) -> dict[str, pa.Table]:
    """Apply the per-source split + projection-union guard, then hand each source
    to DuckDB as a pyarrow.Table (Card 3: pyarrow is the internal currency)."""
    needed = projection_union(tree, aliases)
    by_alias: dict[str, list[exp.Expression]] = {alias: [] for alias in aliases}
    for leaf in where_leaves(tree):
        owners = {c.table for c in leaf.find_all(exp.Column)}
        if len(owners) == 1 and (owner := owners.pop()) in by_alias:
            by_alias[owner].append(leaf)

    tables: dict[str, pa.Table] = {}
    for alias, source in aliases.items():
        registered = REGISTERED[source]
        rows = evaluate_pushed(DATASETS[registered], by_alias[alias])
        columns = sorted(needed[alias])
        tables[registered] = pa.Table.from_pylist(
            [{c: row[c] for c in columns} for row in rows],
            schema=pa.schema([(c, pa.string() if c != "id" else pa.int64()) for c in columns]),
        )
    return tables


# --------------------------------------------------------------------------
# Step 5 — rewrite table refs to the registered names and execute in DuckDB
# --------------------------------------------------------------------------


def rebind_to_registered(tree: exp.Select) -> exp.Select:
    """`github.pull_requests AS pr` -> `github_pull_requests AS pr`, keeping the
    alias so every qualified column still resolves."""
    tree = tree.copy()
    for table in tree.find_all(exp.Table):
        registered = REGISTERED[(table.db, table.name)]
        table.set("db", None)
        table.set("this", exp.to_identifier(registered))
    return tree


def add_order_tiebreaker(tree: exp.Select, alias: str, column: str) -> exp.Select:
    """HLD §9: result ordering is `issue.updated DESC, issue.key ASC`. The
    tiebreaker is not cosmetic — the cursor is an offset, and an offset over a
    non-total order skips or duplicates rows."""
    order = tree.args.get("order")
    if order is None:
        return tree
    order.append("expressions", exp.Ordered(this=exp.column(column, alias), desc=False))
    return tree


def execute(tree: exp.Select, tables: dict[str, pa.Table]) -> pa.Table:
    """Card 3: duckdb.register(name, arrow_table) per source, :memory: per query,
    then run the SQL over the registered names."""
    conn = duckdb.connect(":memory:")
    try:
        for name, table in tables.items():
            conn.register(name, table)
        # SPIKE FINDING: on duckdb 1.5.x `.arrow()` returns a RecordBatchReader, not
        # a Table, and `fetch_arrow_table()` is deprecated. Use `to_arrow_table()`.
        return conn.execute(tree.sql(dialect="duckdb")).to_arrow_table()
    finally:
        conn.close()
