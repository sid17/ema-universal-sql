"""Stage 2 — SQL text in, an attributed and validated AST out.

Four steps, and their order is the subject of ADR-027:

1. **parse** — ``parse_one(sql, read="duckdb", into=exp.Select)``. The ``into=``
   form raises ``ParseError`` on ``INSERT`` / ``UPDATE`` / ``DELETE`` / ``DROP``
   before anything walks the tree, so DDL and DML are rejected for free.
2. **reject** — the node whitelist, on the **raw** tree. See
   :mod:`src.sqlparse.whitelist` for why this cannot come after step 3.
3. **qualify** — stamps every :class:`~sqlglot.exp.Column` with its owning
   table or alias. *Without it a predicate cannot be attributed to a source*, so
   every later stage depends on it. Passing the catalog's schema makes
   ``validate_qualify_columns`` double as free unknown-column validation.
4. **gate + extract** — every referenced table must resolve to a connector the
   tenant has been granted, and then the pieces the later stages need are pulled
   out of the tree.

**The gate in step 4 is the only thing standing there.** Measured (v3-research
Finding 2): ``qualify()`` raises on an unknown *column* but silently accepts an
unknown *table* — ``SELECT s.a FROM slack.msgs s`` passes the optimizer
untouched. Without this gate that query reaches the planner and fails with a
message about missing capabilities rather than "you have not connected Slack".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, SqlglotError
from sqlglot.optimizer.qualify import qualify

from src.models.errors import ApiError, ErrorCode, InvalidQueryError
from src.sqlparse.catalog import Source, SourceCatalog
from src.sqlparse.whitelist import reject_unsupported

#: sqlglot operator node -> the operator string a capability model declares.
#: One mapping, so the planner and the adapters cannot disagree about what ``>``
#: means.
OPERATOR_SQL: Mapping[type[exp.Expression], str] = {
    exp.EQ: "=",
    exp.NEQ: "!=",
    exp.GT: ">",
    exp.GTE: ">=",
    exp.LT: "<",
    exp.LTE: "<=",
}


@dataclass(frozen=True)
class ColumnRef:
    """One column, attributed to the table alias that owns it."""

    table: str
    name: str

    def __str__(self) -> str:
        return f"{self.table}.{self.name}"


@dataclass(frozen=True)
class TableRef:
    """One source the query reads, resolved against the catalog."""

    alias: str
    source: Source

    @property
    def connector_type(self) -> str:
        return self.source.connector_type


@dataclass(frozen=True)
class Predicate:
    """One leaf comparison, with the sqlglot node it came from.

    The node is kept, not just the value, because the engine re-applies every
    predicate authoritatively by executing the AST — re-deriving SQL from
    ``(column, op, value)`` would be a second rendering path that could disagree
    with the first.
    """

    column: ColumnRef
    op: str
    value: Any
    node: exp.Expression


@dataclass(frozen=True)
class ParsedQuery:
    """The caller's query, validated and attributed."""

    ast: exp.Select
    """The qualified tree. **The single source of truth** for every later stage."""

    tables: tuple[TableRef, ...]
    projection: tuple[ColumnRef, ...]
    predicates: Mapping[str, tuple[Predicate, ...]]
    """The user's WHERE at parse time, keyed by table alias.

    **Not what the planner pushes down.** Stage 3 AND-s the RLS predicate into
    ``ast`` *after* this snapshot is taken, so pushing this would push the query
    the caller asked for rather than the one they are entitled to — i.e. exactly
    the leak this design exists to prevent. The planner re-derives from the
    entitled tree. This field is here for the connector gate, for logging and
    for tests that want to see the caller's own filters.
    """

    join_keys: tuple[tuple[ColumnRef, ColumnRef], ...]
    order_by: tuple[tuple[ColumnRef, str], ...]
    limit: int | None
    sql: str
    """The original text, for the audit log — normalized before it is stored."""

    table_by_alias: Mapping[str, TableRef] = field(default_factory=dict)

    @property
    def connectors(self) -> tuple[str, ...]:
        return tuple(sorted({table.connector_type for table in self.tables}))

    @property
    def resources(self) -> tuple[str, ...]:
        return tuple(sorted({table.source.resource for table in self.tables}))


class SQLParser:
    """Turns SQL text into a :class:`ParsedQuery`, or refuses to."""

    def __init__(self, catalog: SourceCatalog) -> None:
        self._catalog = catalog

    def parse_and_validate(self, sql: str, granted_connectors: Iterable[str]) -> ParsedQuery:
        """Run the four steps. Raises 400 ``INVALID_QUERY`` or 403.

        ``granted_connectors`` is the set of connector types the tenant has an
        enabled, active grant for — resolved by the caller from the control
        plane, because this class must not reach for a tenant of its own.
        """
        tree = self._parse(sql)
        reject_unsupported(tree)
        tree = self._qualify(tree)
        tables = self._resolve_tables(tree, frozenset(granted_connectors))
        by_alias = {table.alias: table for table in tables}

        return ParsedQuery(
            ast=tree,
            tables=tables,
            projection=self._projection(tree),
            predicates=self._predicates(tree, by_alias),
            join_keys=self._join_keys(tree),
            order_by=self._order_by(tree),
            limit=self._limit(tree),
            sql=sql,
            table_by_alias=by_alias,
        )

    # -- steps 1-3 --------------------------------------------------------

    @staticmethod
    def _parse(sql: str) -> exp.Select:
        if not sql or not sql.strip():
            raise InvalidQueryError("The query is empty.", detail="EmptyQuery")
        try:
            return sqlglot.parse_one(sql, read="duckdb", into=exp.Select)
        except ParseError as exc:
            # `into=exp.Select` turns every non-SELECT statement into a
            # ParseError too, so this one branch covers both "malformed" and
            # "this is a write, and we are read-only".
            raise InvalidQueryError(
                f"Could not parse the query as a SELECT statement: {exc}",
                detail="ParseError",
            ) from exc

    def _qualify(self, tree: exp.Select) -> exp.Select:
        try:
            return qualify(tree, schema=self._catalog.qualify_schema(), dialect="duckdb")
        except OptimizeError as exc:
            # The common case is an unknown column, which qualify() names.
            raise InvalidQueryError(
                f"Unknown column or table: {exc}", detail="UnknownColumn"
            ) from exc
        except SqlglotError as exc:
            # LAW 4: any other sqlglot failure is still the caller's query being
            # unsupported, not a server fault — but it must not masquerade as
            # the unknown-column case above.
            raise InvalidQueryError(
                f"The query could not be analysed: {exc}", detail="QualifyError"
            ) from exc

    # -- step 4: the gate -------------------------------------------------

    def _resolve_tables(
        self, tree: exp.Select, granted: frozenset[str]
    ) -> tuple[TableRef, ...]:
        """Resolve every table, refusing unknown or ungranted ones.

        Two different refusals, deliberately:

        - an unknown table is ``400 INVALID_QUERY`` — the caller named something
          that does not exist, which is a typo, not an access problem;
        - a known-but-ungranted connector is ``403 CONNECTOR_NOT_ENABLED``,
          naming the connector so an admin knows what to install.

        Collapsing them would mean either telling someone to request access to a
        table that does not exist, or handing an unauthenticated caller a
        catalogue-enumeration oracle. The second is why the unknown-table message
        lists only what the *catalog* knows, never what the tenant is granted.
        """
        refs: list[TableRef] = []
        for table in tree.find_all(exp.Table):
            # (db, name), never catalog: `github.pull_requests` parses as
            # db='github', name='pull_requests', catalog=''.
            source = self._catalog.get(table.text("db"), table.name)
            if source is None:
                named = f"{table.text('db')}.{table.name}" if table.text("db") else table.name
                raise InvalidQueryError(
                    f"Unknown table {named!r}. Available: "
                    f"{', '.join(self._catalog.known_names())}",
                    detail="UnknownTable",
                )
            if source.connector_type not in granted:
                raise ApiError(
                    code=ErrorCode.CONNECTOR_NOT_ENABLED,
                    http=403,
                    message=(
                        f"Connector {source.connector_type!r} is not enabled for this "
                        f"tenant, but the query reads {source.qualified_name}."
                    ),
                    suggested_action=(
                        f"Ask an administrator to connect {source.connector_type} "
                        f"for this tenant."
                    ),
                )
            refs.append(TableRef(alias=table.alias_or_name, source=source))
        return tuple(refs)

    # -- step 4: extraction -----------------------------------------------

    @staticmethod
    def _column_ref(column: exp.Column) -> ColumnRef:
        return ColumnRef(table=column.table, name=column.name)

    @classmethod
    def _projection(cls, tree: exp.Select) -> tuple[ColumnRef, ...]:
        """The selected columns, in order.

        ``qualify`` wraps each projection in an ``Alias``, so the column is one
        level down; ``find_all`` handles both shapes without special-casing.
        """
        refs: list[ColumnRef] = []
        for projection in tree.selects:
            for column in projection.find_all(exp.Column):
                refs.append(cls._column_ref(column))
        return tuple(refs)

    @classmethod
    def _predicates(
        cls, tree: exp.Select, by_alias: Mapping[str, TableRef]
    ) -> Mapping[str, tuple[Predicate, ...]]:
        where = tree.args.get("where")
        if where is None:
            return {}
        grouped: dict[str, list[Predicate]] = {alias: [] for alias in by_alias}
        for leaf in flatten_conjunction(where.this):
            predicate = cls._as_predicate(leaf)
            if predicate is not None and predicate.column.table in grouped:
                grouped[predicate.column.table].append(predicate)
        return {alias: tuple(values) for alias, values in grouped.items() if values}

    @classmethod
    def _as_predicate(cls, leaf: exp.Expression) -> Predicate | None:
        """``column <op> literal`` as a :class:`Predicate`, else ``None``.

        ``None`` covers the join condition (two columns, no literal) and any
        other shape that is not a single-column comparison. Those are not
        errors — they are residual work for the engine.
        """
        op = OPERATOR_SQL.get(type(leaf))
        if op is None:
            return None
        left, right = leaf.this, leaf.expression
        if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
            return Predicate(cls._column_ref(left), op, right.this, leaf)
        return None

    @classmethod
    def _join_keys(cls, tree: exp.Select) -> tuple[tuple[ColumnRef, ColumnRef], ...]:
        keys: list[tuple[ColumnRef, ColumnRef]] = []
        for join in tree.find_all(exp.Join):
            on = join.args.get("on")
            if on is None:
                continue
            for leaf in flatten_conjunction(on):
                if not isinstance(leaf, exp.EQ):
                    continue
                left, right = leaf.this, leaf.expression
                if isinstance(left, exp.Column) and isinstance(right, exp.Column):
                    keys.append((cls._column_ref(left), cls._column_ref(right)))
        return tuple(keys)

    @classmethod
    def _order_by(cls, tree: exp.Select) -> tuple[tuple[ColumnRef, str], ...]:
        order = tree.args.get("order")
        if order is None:
            return ()
        terms: list[tuple[ColumnRef, str]] = []
        for ordered in order.expressions:
            for column in ordered.find_all(exp.Column):
                direction = "DESC" if ordered.args.get("desc") else "ASC"
                terms.append((cls._column_ref(column), direction))
        return tuple(terms)

    @staticmethod
    def _limit(tree: exp.Select) -> int | None:
        limit = tree.args.get("limit")
        if limit is None:
            return None
        try:
            return int(limit.expression.this)
        except (AttributeError, TypeError, ValueError) as exc:
            # LAW 4: a LIMIT we cannot read is not a LIMIT we may ignore —
            # ignoring it would silently return the whole result set.
            raise InvalidQueryError(
                "LIMIT must be a plain integer literal.", detail="Limit"
            ) from exc


def flatten_conjunction(node: exp.Expression):
    """Yield the leaf conjuncts of an AND tree.

    **A single ``where.this.flatten()`` is NOT enough** — the Phase 0 spike's
    headline finding. ``tree.where(pred, append=True)`` routes through
    ``exp.and_``, which wraps the *existing* WHERE in an ``exp.Paren`` before
    AND-ing. ``flatten()`` prunes at the paren and yields the whole nested AND as
    one leaf, so after stage 3 injects RLS the caller's original predicates never
    separate and **nothing is pushable**. Recursing through ``.unnest()`` is what
    makes pushdown survive entitlement injection.

    Lives here rather than in the planner because both the parser and the planner
    need it, and two copies would be two chances to regress the same bug.
    """
    node = node.unnest()
    if isinstance(node, exp.And):
        yield from flatten_conjunction(node.this)
        yield from flatten_conjunction(node.expression)
    else:
        yield node
