"""Stage 5b — everything the caller needs to know how much to trust the answer.

*Knows the contract. Knows nothing about DuckDB* (ADR-034).

The envelope is a first-class deliverable, not decoration (HLD §4): a federated
answer assembled from sources with different freshness, different budgets and
different failure modes is only usable if the caller can see which of those
applied. So this module's whole job is **honesty**:

- ``freshness_ms`` reports the **stalest** contributor, never the average and
  never the newest — a caller comparing it to their tolerance is asking *"is all
  of this fresh enough"*, which is the only safe question about a joined result.
- ``partial`` and ``join_status`` are distinct because "a source was missing" and
  "a JOINED source was missing" have different consequences. The un-joined rows
  of a surviving side are **never** passed off as the joined answer.
- ``next_cursor`` is ``None`` whenever ``partial`` is true. Paging from an
  incomplete page would silently skip the rows the failed source never
  contributed, making the omission invisible exactly when it matters most.
  ``QueryEnvelope`` enforces this structurally, so this module cannot forget it.

**Empty, partial and error stay three distinct shapes** (non-negotiable #3).
Empty is a *correct* answer, partial is a *degraded* one, error is *no* answer —
and a caller must be able to tell them apart without reading prose.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlglot import exp

from src.connectors.pagination import decode_token, encode_token
from src.execution.federation import FederationResult, SourceFetch
from src.governance.clock import NowMs, wall_clock_ms
from src.governance.ratelimit import RateLimitPolicy, TokenBucketRateLimiter
from src.models.envelope import ColumnMeta, ConnectorBudget, QueryEnvelope, SourceOutcome
from src.models.errors import ErrorCode, InvalidQueryError
from src.observability.metrics import set_rate_limit_remaining
from src.planner.planner import QueryPlan

logger = logging.getLogger(__name__)

#: The strategy tag on a result-level cursor.
#:
#: Not cosmetic. It is what makes a *connector*-level GitHub cursor, handed back
#: as a result-level one, get rejected rather than silently reinterpreted as an
#: offset into a different row set — which would return real rows for the wrong
#: question, the most expensive kind of wrong answer.
RESULT_STRATEGY = "result"

#: Page size when the query names no ``LIMIT``.
DEFAULT_PAGE_SIZE = 50


@dataclass(frozen=True)
class Page:
    """Where in the entitled result this response starts."""

    offset: int
    size: int

    @property
    def probe_limit(self) -> int:
        """Rows to ask DuckDB for: the page, plus one to detect a next page.

        Asking for exactly the page size cannot distinguish "the last page" from
        "a full page with more behind it", so the cursor would either always be
        issued (and the final one lead to an empty page) or never be.
        """
        return self.offset + self.size + 1


def decode_cursor(cursor: str | None, limit: int | None) -> Page:
    """Turn the caller's cursor into an offset, or refuse it.

    A malformed cursor is the caller's error and is reported as one. It is
    emphatically **not** silently restarted at offset 0: a caller paging through
    results would get a duplicate page and no indication anything went wrong,
    and would reasonably conclude the *data* was wrong rather than the cursor.
    """
    size = limit if limit and limit > 0 else DEFAULT_PAGE_SIZE
    if cursor is None:
        return Page(offset=0, size=size)
    try:
        return Page(offset=decode_token(cursor, RESULT_STRATEGY), size=size)
    except ValueError as exc:
        raise InvalidQueryError(f"Invalid cursor: {exc}", detail="Cursor") from exc


class ResultAssembler:
    """Builds the :class:`QueryEnvelope` from a federation result."""

    def __init__(
        self,
        limiter: TokenBucketRateLimiter,
        control_plane: Any,
        now_ms: NowMs = wall_clock_ms,
    ) -> None:
        self._limiter = limiter
        self._control_plane = control_plane
        # The SAME injected clock the bucket and the cache take (ADR-019).
        # Reading `time.time()` here instead would make `freshness_ms` the one
        # value in the system that cannot be controlled in a test — and since
        # `fetched_at` comes from the cache, which IS on the injected clock, the
        # two would disagree by whatever the test's clock offset happened to be
        # and produce spurious STALE_DATA warnings on perfectly fresh data.
        self._now_ms = now_ms

    async def assemble(
        self,
        plan: QueryPlan,
        result: FederationResult,
        tenant_id: str,
        trace_id: str,
        max_staleness_ms: int,
        page: Page,
        stats: dict[str, Any] | None = None,
    ) -> QueryEnvelope:
        rows, has_more = self._paginate(result.rows, page)
        partial = bool(result.failed)
        freshness_ms = self._freshness_ms(result.fetches)

        return QueryEnvelope(
            columns=self._columns(plan, result),
            rows=rows,
            freshness_ms=freshness_ms,
            rate_limit_status=await self._budgets(tenant_id, result.fetches),
            sources=self._sources(result.fetches),
            join_status=self._join_status(plan, result),
            partial=partial,
            # None when partial — enforced by QueryEnvelope's own validator too,
            # so neither layer alone is load-bearing.
            next_cursor=None if partial or not has_more else self._cursor(page),
            warnings=self._warnings(result, freshness_ms, max_staleness_ms),
            trace_id=trace_id,
            stats=self._stats(result, stats),
        )

    # -- rows and pagination ----------------------------------------------

    @staticmethod
    def _paginate(rows: list[list[Any]], page: Page) -> tuple[list[list[Any]], bool]:
        """Slice the window out of the (over-fetched) sorted rows."""
        window = rows[page.offset : page.offset + page.size]
        return window, len(rows) > page.offset + page.size

    @staticmethod
    def _cursor(page: Page) -> str:
        return encode_token(RESULT_STRATEGY, page.offset + page.size)

    # -- columns -----------------------------------------------------------

    @staticmethod
    def _columns(plan: QueryPlan, result: FederationResult) -> list[ColumnMeta]:
        """One :class:`ColumnMeta` per output column.

        ``source`` is resolved through the projection's *inner* column, so a
        masked projection (``MD5(issue.reporter_email) AS reporter_email``) is
        still attributed to Jira rather than to nothing. ``type`` comes from
        DuckDB's own result description rather than from the projection, so it
        describes what was actually returned.
        """
        tables = plan.entitled.parsed.table_by_alias
        owner_by_output: dict[str, str] = {}
        masked_outputs: set[str] = set()

        for projection in plan.ast.selects:
            columns = list(projection.find_all(exp.Column))
            if not columns:
                continue
            table = tables.get(columns[0].table)
            if table is None:
                continue
            owner_by_output[projection.output_name] = table.connector_type
            key = f"{table.source.qualified_name}.{columns[0].name}"
            if key in plan.entitled.masks:
                masked_outputs.add(projection.output_name)

        return [
            ColumnMeta(
                name=name,
                type=kind,
                source=owner_by_output.get(name, ""),
                masked=name in masked_outputs,
            )
            for name, kind in result.columns
        ]

    # -- freshness ---------------------------------------------------------

    def _freshness_ms(self, fetches: tuple[SourceFetch, ...]) -> int | None:
        """``now - min(fetched_at)`` — the **stalest** contributor (HLD §9).

        ``None`` when nothing was fetched at all, which is honest: there is no
        data whose age could be reported. Zero would claim perfect freshness for
        an answer that has no contributors.
        """
        stamps = [f.response.fetched_at for f in fetches if f.response is not None]
        if not stamps:
            return None
        # `fetched_at` is epoch SECONDS (the unit HLD §4 fixes for the envelope);
        # the clock is milliseconds. The conversion is here, in one place.
        return max(0, self._now_ms() - int(min(stamps) * 1000))

    # -- provenance --------------------------------------------------------

    @staticmethod
    def _sources(fetches: tuple[SourceFetch, ...]) -> list[SourceOutcome]:
        """``state`` is how the fetch ended; ``served`` is where rows came from.

        Both, so a timeout that was still answered from cache is distinguishable
        from a timeout that returned nothing.
        """
        return [
            SourceOutcome(connector=f.connector_type, state=f.state, served=f.served)
            for f in fetches
        ]

    @staticmethod
    def _join_status(plan: QueryPlan, result: FederationResult) -> str:
        """``complete`` / ``incomplete`` / ``n/a``.

        ``n/a`` is not a hedge — it is the correct answer for a query with no
        join, where "was the join complete" is not a question that has one. A
        multi-source scan with a missing side is still ``partial``, but its
        join status is ``n/a`` because nothing was joined.
        """
        if not plan.entitled.parsed.join_keys:
            return "n/a"
        failed_aliases = {f.alias for f in result.failed}
        joined_aliases = {
            ref.table for pair in plan.entitled.parsed.join_keys for ref in pair
        }
        return "incomplete" if failed_aliases & joined_aliases else "complete"

    def _warnings(
        self,
        result: FederationResult,
        freshness_ms: int | None,
        max_staleness_ms: int,
    ) -> list[dict]:
        warnings: list[dict] = []
        for fetch in result.failed:
            code = fetch.error.code if fetch.error else ErrorCode.SOURCE_TIMEOUT
            warnings.append(
                {
                    "code": code.value,
                    "connector": fetch.connector_type,
                    "message": fetch.error.message if fetch.error else "",
                }
            )

        # STALE_DATA rides back on a 200 (design-doc §8.1): the answer is usable
        # but caveated, and failing the whole query would throw away good rows.
        if (
            freshness_ms is not None
            and max_staleness_ms > 0
            and freshness_ms > max_staleness_ms
        ):
            warnings.append(
                {
                    "code": ErrorCode.STALE_DATA.value,
                    "message": (
                        f"the stalest contributor is {freshness_ms}ms old, beyond the "
                        f"requested {max_staleness_ms}ms; no fresher data was available"
                    ),
                }
            )
        return warnings

    # -- budgets -----------------------------------------------------------

    async def _budgets(
        self, tenant_id: str, fetches: tuple[SourceFetch, ...]
    ) -> dict[str, ConnectorBudget]:
        """Tokens left per connector, read without spending one.

        Reported for every connector the query *touched*, including ones served
        from cache — "we did not need your budget" is exactly what a caller
        watching the rate-limit banner needs to see.
        """
        budgets: dict[str, ConnectorBudget] = {}
        for fetch in fetches:
            row = self._control_plane.get_rate_limit_policy(tenant_id, fetch.connector_type)
            if row is None:
                # No configured budget is a seeding gap, not a runtime failure —
                # the fetch itself would already have refused. Reported as
                # exhausted rather than omitted, so the field never silently
                # disappears from the contract.
                budgets[fetch.connector_type] = ConnectorBudget(remaining=0, throttled=True)
                continue
            remaining = await self._limiter.remaining(
                tenant_id, fetch.connector_type, RateLimitPolicy.from_row(row)
            )
            # The gauge and the envelope get the SAME value from the SAME read
            # (ADR-043). Published here rather than inside the limiter because
            # the limiter deliberately takes no tenant-scoped reporting duty
            # (ADR-020) and is skipped entirely on a cache hit — which would
            # leave `/metrics` asserting a budget that had since moved.
            set_rate_limit_remaining(tenant_id, fetch.connector_type, remaining)
            budgets[fetch.connector_type] = ConnectorBudget(
                remaining=remaining,
                throttled=fetch.state == "throttled" or remaining <= 0,
            )
        return budgets

    # -- stats -------------------------------------------------------------

    @staticmethod
    def _stats(result: FederationResult, stage_ms: Mapping[str, Any] | None) -> dict[str, Any]:
        """Stage timings plus per-connector time.

        ``connector_ms`` carries only sources that made a **live** call. A cache
        hit costing 0.4ms would otherwise appear as connector time and make the
        Phase 4 trace claim the source was fast when it was never called — the
        HLD's own envelope example shows Jira omitted for exactly this reason.
        """
        stats: dict[str, Any] = dict(stage_ms or {})
        connector_ms = {
            fetch.connector_type: round(fetch.elapsed_ms, 2)
            for fetch in result.fetches
            if fetch.served == "live"
        }
        stats["connector_ms"] = connector_ms
        stats["rows_examined"] = len(result.rows)
        return stats
