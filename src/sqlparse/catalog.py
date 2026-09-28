"""What tables exist, and what each one can be asked to do.

The catalog is the single answer to *"is `jira.issues` a real thing, and what
columns does it have?"* — a question three stages ask and must never answer
differently:

- **stage 2** hands the column sets to ``qualify()`` so it can attribute every
  column to its owning table (and reject unknown columns for free);
- **stage 4** reads ``CapabilityModel`` to decide what may be pushed down;
- **stage 5** reads the declared column *types* to build the Arrow schema.

It is built from the **control plane**, never from a literal in the code. An
adapter that could declare its own capabilities could disagree with the control
plane about what is filterable, and the planner reads the control plane — so the
disagreement would surface as a connector rejecting a predicate the planner was
certain it supported.

``github.pull_requests`` parses as ``db='github'``, ``name='pull_requests'``,
``catalog=''`` — so the key is **(db, name)** and never the catalog. That gotcha
is Card 1's, re-confirmed by the v3 probe.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from src.connectors.base import CapabilityModel

#: The SQL type handed to ``qualify()`` for every column.
#:
#: Deliberately uniform: ``qualify`` uses the schema to resolve *names*, not to
#: type-check, and the prototype's subset has no arithmetic or casting for a
#: type to matter in. The types that DO matter — the ones the Arrow schema and
#: ``ColumnMeta`` are built from — come from ``CapabilityModel.type_of``, which
#: is a different question asked at a different stage.
QUALIFY_COLUMN_TYPE = "VARCHAR"


@dataclass(frozen=True)
class Source:
    """One queryable table: where it lives and what it can do."""

    connector_type: str
    """The ``db`` half of ``github.pull_requests`` — and the grant's key."""

    resource: str
    """The ``name`` half. Declared by the adapter class, not by the catalog."""

    capabilities: CapabilityModel

    @property
    def qualified_name(self) -> str:
        return f"{self.connector_type}.{self.resource}"

    @property
    def registered_name(self) -> str:
        """The flat name this source's rows are registered under in DuckDB.

        DuckDB has no ``github`` schema, so ``github.pull_requests`` is rebound
        to ``github_pull_requests`` before execution. Deriving the name here
        rather than in a lookup table means onboarding a connector still adds no
        code outside its own YAML and adapter class.
        """
        return f"{self.connector_type}_{self.resource}"


class SourceCatalog:
    """Every source this process can query, keyed by ``(db, name)``."""

    def __init__(self, sources: Mapping[tuple[str, str], Source]) -> None:
        self._sources = dict(sources)

    def __len__(self) -> int:
        return len(self._sources)

    def get(self, db: str, name: str) -> Source | None:
        """The source for a parsed table reference, or ``None`` if unknown."""
        return self._sources.get((db, name))

    def by_connector(self, connector_type: str) -> Source | None:
        """The single resource a connector serves.

        The prototype's connectors serve exactly one resource each, which is why
        this is unambiguous. A connector with two resources would need the
        resource name here — noted rather than built (LAW 5).
        """
        for source in self._sources.values():
            if source.connector_type == connector_type:
                return source
        return None

    def qualify_schema(self) -> dict[str, dict[str, dict[str, str]]]:
        """The nested ``{db: {table: {column: type}}}`` shape ``qualify()`` wants.

        Passing this is what makes ``validate_qualify_columns`` double as free
        column validation: an unknown column raises ``OptimizeError`` instead of
        surfacing three stages later as an empty fetch.
        """
        schema: dict[str, dict[str, dict[str, str]]] = {}
        for (db, name), source in self._sources.items():
            columns = dict.fromkeys(source.capabilities.columns, QUALIFY_COLUMN_TYPE)
            schema.setdefault(db, {})[name] = columns
        return schema

    def known_names(self) -> list[str]:
        """Every queryable table, sorted — for error messages that help."""
        return sorted(source.qualified_name for source in self._sources.values())
