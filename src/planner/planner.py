"""Stage 4 — what each source is asked for, and what the engine keeps.

Two invariants from `02-DEFINITION-OF-DONE.md` §4 live in this file, and both are
about the same thing: **pushdown is an optimization, never correctness.**

#2a — *the engine re-applies every predicate authoritatively.* It does, in the
most direct way available: the SQL executed in DuckDB is the **whole entitled
tree**, WHERE clause and all. A predicate that was pushed is simply applied
twice, which is free and cannot be wrong. There is no "residual predicate list"
to keep in sync with what was pushed, because a list like that is exactly where
this guarantee would rot.

#2b — *fetch ``projection ∪ every WHERE / ORDER BY / join-key column``.* Without
it the authoritative re-filter would evaluate a predicate against a column that
was never fetched and drop rows the caller was entitled to. Computed from the
tree itself, so it cannot drift from what the tree references.

**Do not reach for sqlglot's ``pushdown_predicates`` / ``pushdown_projections``**
(Card 1). Those passes push *within one AST* — into subqueries and joins — not
out to independent connectors. Wrong abstraction, right-sounding name.

**The split must flatten recursively.** ``tree.where(pred, append=True)``
parenthesizes, and a single ``.flatten()`` prunes at the paren, yielding the
whole nested AND as one leaf. After stage 3 injects RLS that means the caller's
predicates never separate and *nothing is pushable* — while the query still
returns correct rows, because the engine re-applies everything. A silent
collapse to full post-filtering is precisely the bug this design cannot have, so
the flatten lives in one place (``sqlparse.parser.flatten_conjunction``) and is
tested there.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlglot import exp

from src.connectors.base import RequestOption
from src.entitlement.engine import EntitledPlan
from src.models.errors import InvalidQueryError
from src.sqlparse.catalog import Source
from src.sqlparse.parser import OPERATOR_SQL, ColumnRef, TableRef, flatten_conjunction


@dataclass(frozen=True)
class PushedPredicate:
    """One predicate a source will apply for us, and where it is injected."""

    column: str
    op: str
    value: Any
    option: RequestOption
    """The airbyte-derived placement (query string, header, body, path).

    Carried through because knowing GitHub can filter on ``state`` is useless
    without knowing it goes in the query string while ``repo`` goes in the path.
    """


@dataclass(frozen=True)
class SourcePlan:
    """Everything one adapter is asked for."""

    alias: str
    source: Source
    pushed: tuple[PushedPredicate, ...]
    columns_to_fetch: tuple[str, ...]
    not_pushed: tuple[str, ...]
    """Predicates this source cannot apply, kept for the trace and the envelope.

    Reporting only. They are re-applied by the engine like every other
    predicate — including the ones that *were* pushed.
    """

    yields_nothing: bool = False
    """Default-denied: do not call this adapter at all.

    The entitled tree already carries an unsatisfiable predicate for this
    resource, so the *result* would be empty either way. But a constant has no
    owning table, so it cannot be pushed — and the source would be fetched in
    full and then discarded, which is the post-filtering non-negotiable #1
    forbids. Skipping the fetch is what makes "forbidden rows are never
    requested" true for the default-deny case as well as the RLS case.
    """

    @property
    def connector_type(self) -> str:
        return self.source.connector_type

    def fetch_predicates(self) -> dict[str, tuple[str, Any]]:
        """The pushed predicates in ``FetchRequest.predicates`` shape."""
        return {p.column: (p.op, p.value) for p in self.pushed}


@dataclass(frozen=True)
class QueryPlan:
    """The entitled tree, plus how the work divides."""

    entitled: EntitledPlan
    sources: Mapping[str, SourcePlan]
    limit: int | None

    @property
    def ast(self) -> exp.Select:
        """The tree the engine executes — every predicate still in it."""
        return self.entitled.ast


class QueryPlanner:
    """Turns an :class:`EntitledPlan` into per-source fetches."""

    #: How many rows to ask a source for. The prototype's datasets are tens of
    #: rows, so one page is always the whole set; a live adapter would loop on
    #: ``AdapterResponse.next_cursor`` until it had enough. Noted, not built —
    #: connector-level paging beyond one page is a COULD (DoD §3).
    DEFAULT_SOURCE_LIMIT = 100

    def plan(self, entitled: EntitledPlan) -> QueryPlan:
        tree = entitled.ast
        parsed = entitled.parsed

        self._ensure_total_order(tree, parsed.join_keys, parsed.table_by_alias)

        needed = self._projection_union(tree, parsed.table_by_alias)
        leaves_by_alias = self._split(tree, parsed.table_by_alias)

        sources = {}
        for alias, table in parsed.table_by_alias.items():
            sources[alias] = self._plan_source(
                alias,
                table,
                leaves_by_alias.get(alias, ()),
                needed.get(alias, frozenset()),
                yields_nothing=alias in entitled.empty_aliases,
            )
        return QueryPlan(entitled=entitled, sources=sources, limit=parsed.limit)

    # -- invariant #2b ----------------------------------------------------

    @staticmethod
    def _projection_union(
        tree: exp.Select, tables: Mapping[str, TableRef]
    ) -> dict[str, frozenset[str]]:
        """``projection ∪ WHERE ∪ ORDER BY ∪ join keys``, per source.

        Derived by walking the tree for every ``exp.Column``, which is what makes
        it correct by construction: any column the executed SQL can reference is,
        by definition, a column the walk finds. Enumerating the clauses by hand
        would need updating every time a clause was added.

        Runs **after** stage 3, so the CLS-rewritten ``MD5(issue.reporter_email)``
        still contributes ``reporter_email`` to the Jira fetch — masking a column
        does not stop us needing to read it.
        """
        needed: dict[str, set[str]] = {alias: set() for alias in tables}
        for column in tree.find_all(exp.Column):
            if column.table in needed:
                needed[column.table].add(column.name)
        return {alias: frozenset(columns) for alias, columns in needed.items()}

    # -- the per-source split ---------------------------------------------

    @staticmethod
    def _split(
        tree: exp.Select, tables: Mapping[str, TableRef]
    ) -> dict[str, tuple[exp.Expression, ...]]:
        """Group each WHERE leaf by the single table it touches.

        A leaf touching two tables — the join condition — belongs to no source
        and simply is not pushed. That falls out of the grouping rather than
        being special-cased, which is exactly the guard we want: there is no
        code path that could decide to push half a join to GitHub.
        """
        where = tree.args.get("where")
        if where is None:
            return {}
        grouped: dict[str, list[exp.Expression]] = {alias: [] for alias in tables}
        for leaf in flatten_conjunction(where.this):
            owners = {column.table for column in leaf.find_all(exp.Column)}
            if len(owners) == 1:
                owner = owners.pop()
                if owner in grouped:
                    grouped[owner].append(leaf)
        return {alias: tuple(leaves) for alias, leaves in grouped.items()}

    # -- invariant #2a: capability check ----------------------------------

    def _plan_source(
        self,
        alias: str,
        table: TableRef,
        leaves: tuple[exp.Expression, ...],
        needed: frozenset[str],
        yields_nothing: bool = False,
    ) -> SourcePlan:
        capabilities = table.source.capabilities
        pushed: list[PushedPredicate] = []
        not_pushed: list[str] = []

        for leaf in leaves:
            candidate = self._as_candidate(leaf)
            if candidate is None:
                # Not a `column <op> literal` — nothing a source API could be
                # asked for. Residual, like anything else it cannot do.
                not_pushed.append(leaf.sql(dialect="duckdb"))
                continue
            column, op, value = candidate
            if not capabilities.supports(column, op):
                # **Residual, never dropped.** A dropped predicate returns rows
                # the caller did not ask for — and when that predicate is the
                # RLS filter, it is a data leak rather than a bug.
                not_pushed.append(leaf.sql(dialect="duckdb"))
                continue
            pushed.append(
                PushedPredicate(
                    column=column,
                    op=op,
                    value=value,
                    option=capabilities.key_columns[column].option,
                )
            )

        # The required-key check is skipped for a source we will not call: a
        # default-denied caller must get an empty result, not a 400 telling them
        # to add a `repo` filter to a query that was never going to run.
        if not yields_nothing:
            self._require_key_columns(table, pushed)
        columns = self._fetchable(table.source, needed)
        return SourcePlan(
            alias=alias,
            source=table.source,
            pushed=tuple(pushed),
            columns_to_fetch=columns,
            not_pushed=tuple(not_pushed),
            yields_nothing=yields_nothing,
        )

    @staticmethod
    def _as_candidate(leaf: exp.Expression) -> tuple[str, str, Any] | None:
        op = OPERATOR_SQL.get(type(leaf))
        if op is None:
            return None
        left, right = leaf.this, leaf.expression
        if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
            return left.name, op, right.this
        return None

    @staticmethod
    def _require_key_columns(table: TableRef, pushed: list[PushedPredicate]) -> None:
        """A source with no endpoint to call is an error, not a residual.

        GitHub's ``repo`` is a path segment: there is no ``/pulls`` endpoint
        spanning every repository, so a fetch without it has no URL at all. This
        is ``require: required`` in the capability model earning its keep.
        """
        have = {p.column for p in pushed}
        missing = [c for c in table.source.capabilities.required_columns() if c not in have]
        if missing:
            raise InvalidQueryError(
                f"{table.source.qualified_name} requires an equality filter on "
                f"{', '.join(missing)} — the upstream API has no endpoint without it. "
                f"Add it to your WHERE clause.",
                detail="MissingRequiredFilter",
            )

    @staticmethod
    def _fetchable(source: Source, needed: frozenset[str]) -> tuple[str, ...]:
        """The union, narrowed to columns the source actually has, sorted.

        Sorted so the connector cache key is stable across requests that
        reference the same columns in a different order — an unsorted set would
        fragment the cache for queries that are identical in every way that
        matters.
        """
        declared = set(source.capabilities.columns)
        return tuple(sorted(needed & declared))

    # -- total ordering (HLD §9) ------------------------------------------

    @staticmethod
    def _ensure_total_order(
        tree: exp.Select,
        join_keys: tuple[tuple[ColumnRef, ColumnRef], ...],
        tables: Mapping[str, TableRef],
    ) -> None:
        """Append a tiebreaker so the result order is **total**.

        HLD §9 fixes the result ordering at ``issue.updated DESC, issue.key ASC``
        and the second term is not cosmetic: the result cursor is an offset, and
        an offset over a non-total order lets tied rows reorder between pages, so
        rows are skipped or duplicated. Two of alice's three issues share an
        ``updated`` value precisely so this is exercised (ADR-035).

        The tiebreaker is a **join key**, because a join key is the closest thing
        to a primary key the catalog knows about. With no ORDER BY at all the
        join keys become the ordering outright, so a cursor is always issued over
        a deterministic sequence.
        """
        candidates = [ref for pair in join_keys for ref in pair if ref.table in tables]
        if not candidates:
            # No join, so no join key to break ties with — and the catalog
            # declares no primary key. Fall back to ordering by every projected
            # column, which IS a total order over the *result*: if two rows agree
            # on every column the caller can see, they are indistinguishable, so
            # their relative order cannot skip or duplicate anything a caller
            # could notice. Cheaper orderings exist; none of them is correct
            # without a declared key.
            candidates = [
                ColumnRef(column.table, column.name)
                for projection in tree.selects
                for column in projection.find_all(exp.Column)
                if column.table in tables
            ]
        if not candidates:
            return
        candidates = list(dict.fromkeys(candidates))

        order = tree.args.get("order")
        already = (
            {(c.table, c.name) for term in order.expressions for c in term.find_all(exp.Column)}
            if order is not None
            else set()
        )

        if order is None:
            tree.set(
                "order",
                exp.Order(
                    expressions=[
                        exp.Ordered(this=exp.column(ref.name, ref.table), desc=False)
                        for ref in candidates
                    ]
                ),
            )
            return

        # Prefer the key belonging to the table already being ordered by; that
        # is the one whose ties actually need breaking.
        ordered_tables = {c.table for term in order.expressions for c in term.find_all(exp.Column)}
        preferred = [ref for ref in candidates if ref.table in ordered_tables] or candidates
        for ref in preferred:
            if (ref.table, ref.name) in already:
                continue
            order.append(
                "expressions",
                exp.Ordered(this=exp.column(ref.name, ref.table), desc=False),
            )
            already.add((ref.table, ref.name))
