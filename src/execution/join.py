"""Stage 5a, second half — register the fetched rows and run the entitled SQL.

Split out of :mod:`src.execution.federation` when that module crossed LAW 1's
400-line decompose threshold. The seam is a real one rather than a line-count
convenience: *fetching* is about connectors, deadlines and partial failure;
*joining* is about Arrow schemas and DuckDB. They share only the
:class:`~src.execution.federation.SourceFetch` records that pass between them.

**The SQL executed is the whole entitled tree** — every predicate, including the
ones a source already applied. Re-applying them is free and is what makes
pushdown an optimization rather than a correctness dependency (non-negotiable
#2). Nothing in this module may filter rows on its own authority.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import duckdb
from sqlglot import exp

from src.execution.arrow import build_table, envelope_type
from src.observability.tracing import ELAPSED_MS_ATTRIBUTE, get_tracer
from src.planner.planner import QueryPlan

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, types only
    from src.execution.federation import SourceFetch

#: The DuckDB execution, spanned separately so the waterfall can distinguish
#: "the sources were slow" from "the join was slow" — the distinction the
#: phase's own "Done when" sentence turns on (ADR-037).
JOIN_SPAN = "duckdb_join"


def join_sources(
    plan: QueryPlan,
    fetches: tuple[SourceFetch, ...],
    row_limit: int | None = None,
) -> tuple[list[list[Any]], tuple[tuple[str, str], ...]]:
    """Register every source as an Arrow table and run the entitled SQL.

    Wrapped in the ``duckdb_join`` span so a reader of the waterfall can tell
    "the sources were slow" from "the join was slow" — without it both live
    inside one opaque ``federation`` bar and the phase's headline reading is
    unavailable.
    """
    with get_tracer().start_as_current_span(JOIN_SPAN) as span:
        started = time.perf_counter()
        try:
            return _execute_join(plan, fetches, row_limit)
        finally:
            span.set_attribute(ELAPSED_MS_ATTRIBUTE, (time.perf_counter() - started) * 1000.0)


def _execute_join(
    plan: QueryPlan,
    fetches: tuple[SourceFetch, ...],
    row_limit: int | None,
) -> tuple[list[list[Any]], tuple[tuple[str, str], ...]]:
    """Register, execute, read back.

    Separate from :func:`join_sources` only so the span wraps a call rather
    than a forty-line body — the timing and the work stay legible apart.
    """
    connection = duckdb.connect(":memory:")
    try:
        for fetch in fetches:
            source = fetch.plan.source
            connection.register(
                source.registered_name,
                build_table(
                    fetch.rows,
                    fetch.plan.columns_to_fetch,
                    {
                        column: source.capabilities.type_of(column)
                        for column in fetch.plan.columns_to_fetch
                    },
                ),
            )
        tree = rebind_to_registered(plan.ast)
        if row_limit is not None:
            tree.set("limit", exp.Limit(expression=exp.Literal.number(row_limit)))
        sql = tree.sql(dialect="duckdb")
        cursor = connection.execute(sql)
        columns = tuple(
            (name, envelope_type(kind)) for name, kind, *_ in cursor.description or ()
        )
        # `to_arrow_table()`, not `.arrow()`: on duckdb 1.5.x the latter
        # returns a RecordBatchReader and `fetch_arrow_table()` is deprecated.
        table = cursor.to_arrow_table()
        rows = [[record[name] for name, _ in columns] for record in table.to_pylist()]
        return rows, columns
    finally:
        connection.close()


def rebind_to_registered(tree: exp.Select) -> exp.Select:
    """``github.pull_requests AS pr`` -> ``github_pull_requests AS pr``.

    DuckDB has no ``github`` schema, so the two-part name has to collapse — but
    the **alias must survive**, or every qualified column in the query stops
    resolving. Works on a copy: the caller's tree is what the audit log and the
    trace describe, and rewriting it in place would make both describe DuckDB's
    private naming instead of the query that was asked.
    """
    tree = tree.copy()
    for table in tree.find_all(exp.Table):
        db = table.text("db")
        if not db:
            continue
        table.set("db", None)
        table.set("this", exp.to_identifier(f"{db}_{table.name}"))
    return tree
