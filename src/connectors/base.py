"""The connector contract: capabilities in, rows out.

Two ideas adopted from ``airbyte-python-cdk`` (research Card 2), because they
are what let one contract describe two sources that page and filter differently:

1. **One injection primitive.** :class:`RequestOption` says *where* a value goes
   in the outgoing call — query string, header, body or path. The same primitive
   carries a pushed-down predicate, the page token and the page size, so there
   is no separate vocabulary per concern.
2. **A capability is (predicate support) + (where to inject it).** Knowing that
   GitHub can filter on ``state`` is useless without knowing it goes in the
   query string while ``repo`` goes in the path. :class:`CapabilityModel` carries
   both, which is what makes Phase 2's pushdown split mechanical.

These are plain dataclasses, not Pydantic models: they never cross an HTTP
boundary. Only :mod:`src.models.envelope` is serialized to a caller, so paying
validation cost on every fetch would buy nothing.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

#: Where a value is injected into the outgoing call.
InjectInto = Literal["query", "header", "body", "path"]

#: Whether a source *demands* a predicate on a column, or merely accepts one.
#: ``required`` is load-bearing: GitHub's ``repo`` is a path segment, so a fetch
#: without it has no URL to call at all.
Requirement = Literal["required", "optional"]


@dataclass(frozen=True)
class RequestOption:
    """Where one value goes in the outgoing request."""

    inject_into: InjectInto
    field: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RequestOption":
        return cls(inject_into=raw["inject_into"], field=raw["field"])


@dataclass(frozen=True)
class ColumnCapability:
    """What one column supports: which operators, and where the value goes."""

    require: Requirement
    ops: tuple[str, ...]
    option: RequestOption

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ColumnCapability":
        return cls(
            require=raw.get("require", "optional"),
            ops=tuple(raw["ops"]),
            option=RequestOption.from_dict(raw["option"]),
        )


@dataclass(frozen=True)
class PaginationSpec:
    """Pagination as *strategy* ⊕ *placement*, deliberately decoupled.

    ``strategy`` decides how the next token is computed (cursor, offset, page);
    ``token_option`` decides where that token is injected. A source can change
    one without the other — which is exactly the difference between GitHub's
    cursor and Jira's ``startAt``.
    """

    strategy: Literal["cursor", "offset", "page"]
    page_size: int
    token_option: RequestOption
    stop: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PaginationSpec":
        return cls(
            strategy=raw["strategy"],
            page_size=int(raw["page_size"]),
            token_option=RequestOption.from_dict(raw["token_option"]),
            stop=raw["stop"],
        )


@dataclass(frozen=True)
class CapabilityModel:
    """What a source can be asked to do, as stored in ``connectors.capabilities``.

    Built **from the seeded dict**, never hand-written in the adapter, so the
    adapter and the control plane cannot disagree about what is filterable.
    Phase 2's planner reads the same dict to decide what to push down.
    """

    columns: tuple[str, ...]
    key_columns: Mapping[str, ColumnCapability]
    sortable: tuple[str, ...]
    pagination: PaginationSpec

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CapabilityModel":
        return cls(
            columns=tuple(raw["columns"]),
            key_columns={
                name: ColumnCapability.from_dict(spec) for name, spec in raw["key_columns"].items()
            },
            sortable=tuple(raw.get("sortable", ())),
            pagination=PaginationSpec.from_dict(raw["pagination"]),
        )

    def required_columns(self) -> tuple[str, ...]:
        """Columns a fetch cannot omit a predicate for."""
        return tuple(name for name, cap in self.key_columns.items() if cap.require == "required")

    def supports(self, column: str, op: str) -> bool:
        """Can this source filter ``column`` with ``op``?"""
        cap = self.key_columns.get(column)
        return cap is not None and op in cap.ops


@dataclass(frozen=True)
class AdapterResponse:
    """One page of rows, plus everything the envelope needs to stay honest.

    ``fetched_at`` is epoch **seconds** (the envelope derives ``freshness_ms``
    from it) while every governance primitive works in milliseconds — the
    boundary is here, deliberately and in one place, because HLD §4 fixes the
    field's unit.
    """

    rows: list[dict[str, Any]]
    fetched_at: float
    served: Literal["live", "cache"]
    etag: str | None = None
    next_cursor: str | None = None
    has_more: bool = False
    #: Populated only when the rows came from a conditional request that the
    #: source answered ``304``. Distinguishes "revalidated, still fresh" from
    #: "read straight out of the cache" — both are ``served="cache"``, but only
    #: one of them talked to the source.
    revalidated: bool = False


@dataclass(frozen=True)
class FetchRequest:
    """One fetch, in the shape the cache key is built from.

    Exists so the cache key and the fetch take the *same* object: a key built
    from a separately-assembled dict could drift from what was actually fetched,
    and a cache key that does not describe its payload is a correctness bug, not
    a performance one.
    """

    tenant_id: str
    entitlement_scope: str
    predicates: Mapping[str, Any] = field(default_factory=dict)
    projection: Sequence[str] = ()
    page: str | None = None
    limit: int = 100

    max_staleness_ms: int = 60_000
    """How old cached rows may be and still satisfy *this* caller.

    Deliberately **excluded from the cache key**: it describes the read, not the
    data. Two callers asking the identical question with different staleness
    tolerances must share one entry — otherwise a strict caller would write a
    second copy that a lenient caller could never find, and the cache would
    fragment per-tolerance instead of per-question.
    """


class BaseConnectorAdapter(ABC):
    """The single seam a live adapter reimplements.

    Subclasses implement :meth:`_fetch_live`; :meth:`fetch` itself is concrete
    and owns the ordering that makes the system multi-tenant-safe. That split is
    deliberate — see :meth:`fetch`.
    """

    connector_type: str

    @abstractmethod
    def capabilities(self) -> CapabilityModel:
        """What this source can be asked to do."""

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Liveness detail for ``/healthz``."""

    @abstractmethod
    async def fetch(self, request: FetchRequest) -> AdapterResponse:
        """Return one page of rows for ``request``."""
