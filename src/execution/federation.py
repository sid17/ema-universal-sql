"""Stage 5a — call the sources in parallel, then join what came back in DuckDB.

*Knows DuckDB. Knows nothing about the envelope* (ADR-034).

**The deadline is enforced here, and it had to be.** Phase 0 set
``request.state.deadline_ms`` in ``routes.py`` and nothing ever read it — carried
forward as an explicit watch-out. Without enforcement, "timeouts degrade to
partial results" (brief line 84) would be demonstrated only by a forced-failure
hook we trigger ourselves, while a genuinely slow source hung the request.

**``return_exceptions=True`` is load-bearing, not defensive style.**
``asyncio.gather``'s default propagates the first exception *and cancels its
siblings*. So a Jira timeout would cancel the in-flight GitHub fetch and throw
away rows we already had — converting a ``partial`` answer into an ``error``
one. That is the trichotomy collapsing, which is the one thing
`02-DEFINITION-OF-DONE.md` §4 says may not happen.

**Which failures degrade, and which stop the query.** Not a new rule: it is read
straight off design-doc §8.1, which gives exactly two codes a ``200`` form —
``STALE_DATA`` (a warning) and ``SOURCE_TIMEOUT`` (partial). The other four are
``4xx`` hard stops with no partial answer to offer (§4.5). Note this deliberately
differs from :attr:`~src.connectors.errors.Classification.degradable` for a
throttle: a throttle genuinely *is* transient — retrying later works — but the
product answer is a ``429`` with ``Retry-After`` and the async reroute, not a
quietly smaller result set. "Retryable" and "may be served as partial" are two
different questions, and conflating them would silently turn the rate-limit demo
into a partial result.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import duckdb
from sqlglot import exp

from src.connectors.base import AdapterResponse, BaseConnectorAdapter, FetchRequest
from src.execution.arrow import build_table, envelope_type
from src.models.errors import ApiError, ErrorCode
from src.planner.planner import QueryPlan, SourcePlan

logger = logging.getLogger(__name__)

#: How a source's fetch ended. Mirrors ``SourceOutcome.state`` in the envelope.
SourceState = Literal["ok", "timeout", "error", "throttled"]

#: The only failure a query may survive as a partial result.
#:
#: Derived from design-doc §8.1: it is the one error code the table gives a
#: ``200 (partial)`` form. Everything else is a hard stop there and is a hard
#: stop here.
DEGRADABLE_CODES = frozenset({ErrorCode.SOURCE_TIMEOUT})

#: How many rows to ask each source for.
#:
#: Mirrors ``QueryPlanner.DEFAULT_SOURCE_LIMIT`` and the connectors' declared
#: ``page_size``. The prototype's datasets are tens of rows, so one page is
#: always the whole set; a live adapter would loop on
#: ``AdapterResponse.next_cursor`` until it had enough rows to satisfy the join.
#: Noted rather than built — connector paging beyond one page is a COULD.
DEFAULT_SOURCE_LIMIT = 100

#: Error code -> the ``SourceOutcome.state`` a caller sees.
#:
#: A table rather than a chain of conditionals, so the value is a
#: :data:`SourceState` by construction. The version this replaced built a plain
#: ``str`` and needed a ``type: ignore`` to assign it — silencing the checker on
#: exactly the field whose whole job is to be one of four known words (LAW 7:
#: fix the code, never weaken the config).
SOURCE_STATE_FOR_CODE: Mapping[ErrorCode, SourceState] = {
    ErrorCode.SOURCE_TIMEOUT: "timeout",
    ErrorCode.RATE_LIMIT_EXHAUSTED: "throttled",
}

#: Share of the request deadline one source may spend.
#:
#: Sources run concurrently, so this is not a division of the budget between
#: them — each gets the same slice. The remainder is headroom for the DuckDB
#: join and envelope assembly, which happen *after* the slowest source returns
#: and would otherwise push the whole request past its deadline having done
#: everything right.
SOURCE_BUDGET_FRACTION = 0.8


@dataclass(frozen=True)
class SourceFetch:
    """What happened at one source."""

    alias: str
    connector_type: str
    plan: SourcePlan
    elapsed_ms: float
    state: SourceState = "ok"
    response: AdapterResponse | None = None
    error: ApiError | None = None

    @property
    def rows(self) -> list[dict[str, Any]]:
        return list(self.response.rows) if self.response is not None else []

    @property
    def served(self) -> Literal["live", "cache", "none"]:
        return self.response.served if self.response is not None else "none"


@dataclass(frozen=True)
class FederationResult:
    """Raw joined rows plus the provenance the assembler turns into an envelope."""

    rows: list[list[Any]]
    columns: tuple[tuple[str, str], ...]
    """``(name, envelope type)`` per output column, read from DuckDB's own
    result description rather than guessed from the projection."""

    fetches: tuple[SourceFetch, ...] = field(default_factory=tuple)

    @property
    def failed(self) -> tuple[SourceFetch, ...]:
        return tuple(fetch for fetch in self.fetches if fetch.state != "ok")


class FederationEngine:
    """Fetches every source in parallel and executes the residual SQL."""

    def __init__(
        self,
        adapters: Mapping[str, BaseConnectorAdapter],
        deadline_ms: int,
    ) -> None:
        self._adapters = adapters
        self._deadline_ms = deadline_ms

    async def execute(
        self,
        plan: QueryPlan,
        tenant_id: str,
        max_staleness_ms: int,
        row_limit: int | None = None,
        source_limit: int = DEFAULT_SOURCE_LIMIT,
    ) -> FederationResult:
        """Fetch, then join. ``row_limit`` overrides the query's own ``LIMIT``.

        The override exists because ``LIMIT`` is a **page size**, not a result
        cap: the caller pages through the entitled result with ``next_cursor``,
        so DuckDB must be asked for ``offset + page_size + 1`` rows rather than
        for the page size alone. The runner computes it; this stage applies it.
        """
        fetches = await self._fetch_all(plan, tenant_id, max_staleness_ms, source_limit)
        self._raise_on_hard_failure(fetches)
        rows, columns = self._join(plan, fetches, row_limit)
        return FederationResult(rows=rows, columns=columns, fetches=fetches)

    # -- the parallel fetch, with a real deadline --------------------------

    async def _fetch_all(
        self,
        plan: QueryPlan,
        tenant_id: str,
        max_staleness_ms: int,
        source_limit: int,
    ) -> tuple[SourceFetch, ...]:
        budget_s = (self._deadline_ms * SOURCE_BUDGET_FRACTION) / 1000.0
        items = list(plan.sources.items())

        async def run(alias: str, source_plan: SourcePlan) -> SourceFetch:
            if source_plan.yields_nothing:
                # Default-denied: never call the adapter. The rows this caller
                # is not entitled to are not fetched-and-dropped, they are not
                # requested (non-negotiable #1). `state="ok"` because nothing
                # went wrong — an empty answer is the correct answer, and
                # marking it `error` would make default-deny look like an outage
                # and set `partial`.
                logger.info(
                    "source skipped: no matching allow policy for this caller",
                    extra={"context": {"connector": source_plan.connector_type}},
                )
                return SourceFetch(
                    alias=alias,
                    connector_type=source_plan.connector_type,
                    plan=source_plan,
                    elapsed_ms=0.0,
                    state="ok",
                )

            request = FetchRequest(
                tenant_id=tenant_id,
                entitlement_scope=plan.entitled.entitlement_scope,
                predicates=source_plan.fetch_predicates(),
                projection=source_plan.columns_to_fetch,
                limit=source_limit,
                max_staleness_ms=max_staleness_ms,
            )
            adapter = self._adapter_for(source_plan)
            started = time.perf_counter()
            try:
                response = await asyncio.wait_for(adapter.fetch(request), timeout=budget_s)
            except TimeoutError:
                return self._timed_out(alias, source_plan, started, budget_s)
            except ApiError as exc:
                return self._failed(alias, source_plan, started, exc)
            return SourceFetch(
                alias=alias,
                connector_type=source_plan.connector_type,
                plan=source_plan,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                state="ok",
                response=response,
            )

        # return_exceptions=True: see the module docstring. A sibling failure
        # must never cancel a fetch that is about to succeed.
        results = await asyncio.gather(
            *(run(alias, source_plan) for alias, source_plan in items),
            return_exceptions=True,
        )

        fetches: list[SourceFetch] = []
        for (alias, source_plan), result in zip(items, results, strict=True):
            if isinstance(result, SourceFetch):
                fetches.append(result)
                continue
            # LAW 4: an unexpected exception is our bug. It is logged in full
            # and turned into an explicit `error` outcome — never swallowed, and
            # never quietly reported as a timeout, which would tell a caller to
            # retry something that will fail identically.
            logger.exception(
                "connector fetch raised an unexpected error",
                exc_info=result if isinstance(result, BaseException) else None,
                extra={"context": {"connector": source_plan.connector_type}},
            )
            fetches.append(
                self._failed(
                    alias,
                    source_plan,
                    time.perf_counter(),
                    ApiError(
                        code=ErrorCode.SOURCE_TIMEOUT,
                        http=500,
                        message=f"{source_plan.connector_type}: {result}",
                    ),
                )
            )
        return tuple(fetches)

    def _adapter_for(self, source_plan: SourcePlan) -> BaseConnectorAdapter:
        adapter = self._adapters.get(source_plan.connector_type)
        if adapter is None:
            # A wiring bug, not a caller problem. Failing loudly beats degrading
            # to a partial result that hides a missing adapter forever.
            raise RuntimeError(
                f"no adapter registered for connector {source_plan.connector_type!r}"
            )
        return adapter

    @staticmethod
    def _timed_out(
        alias: str, source_plan: SourcePlan, started: float, budget_s: float
    ) -> SourceFetch:
        logger.warning(
            "connector exceeded its slice of the request deadline",
            extra={
                "context": {
                    "connector": source_plan.connector_type,
                    "budget_ms": int(budget_s * 1000),
                }
            },
        )
        return SourceFetch(
            alias=alias,
            connector_type=source_plan.connector_type,
            plan=source_plan,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            state="timeout",
            error=ApiError(
                code=ErrorCode.SOURCE_TIMEOUT,
                http=504,
                message=(
                    f"{source_plan.connector_type} did not answer within its "
                    f"{int(budget_s * 1000)}ms slice of the request deadline"
                ),
            ),
        )

    @staticmethod
    def _failed(
        alias: str, source_plan: SourcePlan, started: float, exc: ApiError
    ) -> SourceFetch:
        return SourceFetch(
            alias=alias,
            connector_type=source_plan.connector_type,
            plan=source_plan,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            state=SOURCE_STATE_FOR_CODE.get(exc.code, "error"),
            error=exc,
        )

    @staticmethod
    def _raise_on_hard_failure(fetches: tuple[SourceFetch, ...]) -> None:
        """Re-raise the first non-degradable failure as the query's outcome.

        A permanently broken connector presented as a merely ``partial`` result
        would tell the caller to retry something that cannot succeed, and would
        make a revoked credential look like a slow API.
        """
        for fetch in fetches:
            if fetch.error is not None and fetch.error.code not in DEGRADABLE_CODES:
                raise fetch.error

    # -- the join ----------------------------------------------------------

    def _join(
        self,
        plan: QueryPlan,
        fetches: tuple[SourceFetch, ...],
        row_limit: int | None = None,
    ) -> tuple[list[list[Any]], tuple[tuple[str, str], ...]]:
        """Register every source as an Arrow table and run the entitled SQL.

        The SQL executed is the **whole entitled tree** — every predicate,
        including the ones a source already applied. Re-applying them is free and
        is what makes pushdown an optimization rather than a correctness
        dependency (non-negotiable #2).
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
