"""The response envelope — the provenance rail every later phase fills.

This contract is locked: it is the published response shape, so changing a field
name or type is an API break, not a refactor.
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ColumnMeta(BaseModel):
    """One output column, with the source it came from and its masking state."""

    name: str
    type: str
    source: str
    masked: bool = False


class ConnectorBudget(BaseModel):
    """A tenant's remaining rate-limit budget against one connector."""

    remaining: int
    throttled: bool


class SourceOutcome(BaseModel):
    """What actually happened at one source — the honesty half of the rail.

    ``state`` is how the fetch ended; ``served`` is where the rows came from,
    so a ``timeout`` served from ``cache`` is distinguishable from a timeout
    that returned nothing at all.
    """

    connector: str
    state: Literal["ok", "timeout", "error", "throttled"]
    served: Literal["live", "cache", "none"]


class QueryEnvelope(BaseModel):
    """The single response shape for ``POST /v1/query``.

    ``freshness_ms`` is ``now - min(fetched_at)`` across every contributing
    source — i.e. the age of the **stalest** contributor, not the average and
    not the newest. A caller comparing it against ``max_staleness_ms`` is
    therefore asking "is *all* of this fresh enough", which is the only
    question that is safe to answer for a joined result.
    """

    columns: list[ColumnMeta] = Field(default_factory=list)
    rows: list[list] = Field(default_factory=list)
    freshness_ms: int | None = None
    rate_limit_status: dict[str, ConnectorBudget] = Field(default_factory=dict)
    sources: list[SourceOutcome] = Field(default_factory=list)
    join_status: Literal["complete", "incomplete", "n/a"] = "n/a"
    partial: bool = False
    next_cursor: str | None = None
    warnings: list[dict] = Field(default_factory=list)
    trace_id: str
    stats: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_cursor_when_partial(self) -> "QueryEnvelope":
        """A partial result must not hand back a cursor.

        Paging from a partial page would silently skip the rows the failed
        source never contributed, so the omission becomes invisible.
        """
        if self.partial and self.next_cursor is not None:
            raise ValueError("next_cursor must be None when partial is True")
        return self
