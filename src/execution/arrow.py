"""Rows in, a registerable ``pyarrow.Table`` out.

Small and separate because it is the one place a **measured landmine** is
defused, and burying that in the middle of the federation engine would make it
look incidental:

    >>> pa.Table.from_pylist([])          # infers ZERO columns
    >>> con.register("t", empty)
    InvalidInputException: Provided table/dataframe must have at least one column

``from_pylist`` infers its schema *from the rows*, so an empty result infers
nothing and DuckDB refuses to register it. Every zero-row path reaches
this — a denied resource, an RLS binding that matches nothing, a timed-out source
contributing no rows to a partial answer. carol, the ``empty`` leg of the
trichotomy, is one join away from it.

So the schema is **always** built explicitly from the capability model's declared
column types, never inferred — not only when the rows happen to be
empty. Inference-when-non-empty would be worse than either consistent choice: the
same source would register as ``int64`` on a request that returned data and
``string`` on one that did not, and DuckDB would see a source whose schema
changes request to request.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pyarrow as pa

#: Capability-declared type -> Arrow type.
#:
#: Deliberately small. The mock sources have exactly two shapes (text and
#: GitHub's integer ``number``), and a type system richer than the data is a
#: speculative abstraction. An undeclared type falls back to string,
#: which is the capability model's own default.
ARROW_TYPES: Mapping[str, pa.DataType] = {
    "string": pa.string(),
    "integer": pa.int64(),
    "number": pa.float64(),
    "boolean": pa.bool_(),
    "timestamp": pa.string(),
}

#: DuckDB result type -> the name that goes into ``ColumnMeta.type``.
#: The envelope speaks a small, stable vocabulary,
#: not DuckDB's internal spelling.
ENVELOPE_TYPES: Mapping[str, str] = {
    "VARCHAR": "string",
    "BIGINT": "integer",
    "INTEGER": "integer",
    "HUGEINT": "integer",
    "DOUBLE": "number",
    "FLOAT": "number",
    "BOOLEAN": "boolean",
    "DATE": "string",
    "TIMESTAMP": "string",
}


def arrow_type(declared: str) -> pa.DataType:
    """The Arrow type for a capability-declared column type."""
    return ARROW_TYPES.get(declared, pa.string())


def envelope_type(duckdb_type: Any) -> str:
    """The ``ColumnMeta.type`` name for a DuckDB result column type."""
    return ENVELOPE_TYPES.get(str(duckdb_type).upper(), "string")


def build_table(
    rows: Sequence[Mapping[str, Any]],
    columns: Sequence[str],
    types: Mapping[str, str],
) -> pa.Table:
    """A table with exactly ``columns``, typed from ``types``, however few rows.

    Every row is rebuilt against ``columns`` rather than passed through, so a
    source that omitted a column (projection drops keys a row does not
    carry) contributes ``None`` instead of a ragged record that Arrow would
    reject — or worse, quietly widen.
    """
    schema = pa.schema([(column, arrow_type(types.get(column, "string"))) for column in columns])
    normalized = [{column: row.get(column) for column in columns} for row in rows]
    return pa.Table.from_pylist(normalized, schema=schema)
