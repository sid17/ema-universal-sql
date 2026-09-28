"""The pipeline: five stages, in one order, in one place.

This module owns *sequence* and nothing else. Every stage's logic lives in its
own package; putting the order here means a reader can answer "what happens to a
query" from thirty lines, and means the order cannot drift between the route,
the tests and the demo.

**The order is the design.** ``parse → entitle → plan`` — entitlement is injected
*before* the pushdown split, so the RLS predicate rides pushdown to the source
and the forbidden rows are never requested. The design doc numbers Planner as hop
2 and Entitlement as hop 3, but its §3.2 requires RLS to *"push down with the
query"*, which only one of those orders achieves. The hop numbers are logical
roles, not a sequencing claim.

**Each stage is wrapped in a span as it is written**, not retrofitted in Phase 4
(`03-BUILD-PROCESS.md`'s front-load-observability decision). The person who knows
where a stage begins and ends is the person writing it, and the same timings
land in ``QueryEnvelope.stats`` — so the trace and the envelope cannot disagree
about where the time went.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from src.entitlement.engine import EntitlementEngine
from src.execution.assemble import DEFAULT_PAGE_SIZE, ResultAssembler, decode_cursor
from src.execution.federation import FederationEngine
from src.governance.audit import AuditLogger, AuditRecord, normalize_sql
from src.models.context import UserContext
from src.models.envelope import QueryEnvelope
from src.models.request import QueryRequest
from src.observability.tracing import ELAPSED_MS_ATTRIBUTE, get_tracer
from src.pipeline.registry import ConnectorRegistry
from src.planner.planner import QueryPlanner
from src.sqlparse.parser import SQLParser

logger = logging.getLogger(__name__)


@contextmanager
def stage(name: str, stats: dict[str, Any]) -> Iterator[None]:
    """Time one stage into both a span and ``stats``.

    A context manager rather than :func:`~src.observability.tracing.stage_span`,
    which is a decorator: these stages are steps inside one function, not
    functions of their own, and splitting them into five private methods purely
    to hang a decorator on each would make the order harder to read — which is
    the one thing this module exists to make easy.
    """
    with get_tracer().start_as_current_span(name) as span:
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - started) * 1000.0
            span.set_attribute(ELAPSED_MS_ATTRIBUTE, elapsed)
            stats[f"{name}_ms"] = round(elapsed, 2)


class QueryPipelineRunner:
    """Runs one query through all five stages and assembles the envelope."""

    def __init__(
        self,
        registry: ConnectorRegistry,
        repository: Any,
        assembler: ResultAssembler,
        audit: AuditLogger,
        deadline_ms: int,
    ) -> None:
        self._registry = registry
        #: Public so the TEST_MODE-only `fail-next` route can arm a failure.
        #: The route needs a process-lived object to talk to, and the registry
        #: is the only one — adapters are built per request.
        self.registry = registry
        self._repository = repository
        self._assembler = assembler
        self._audit = audit
        self._deadline_ms = deadline_ms

    async def run(
        self, request: QueryRequest, user: UserContext, trace_id: str
    ) -> QueryEnvelope:
        stats: dict[str, Any] = {}
        started = time.perf_counter()

        with stage("parse", stats):
            catalog = self._registry.catalog()
            granted = self._registry.granted_connectors(user.tenant_id)
            parsed = SQLParser(catalog).parse_and_validate(request.sql, granted)

        with stage("entitlement", stats):
            entitled = EntitlementEngine(self._repository).compile(parsed, user)

        with stage("plan", stats):
            plan = QueryPlanner().plan(entitled)
            # Decoded here, not in the route: the offset decides how many rows
            # DuckDB is asked for, so it is a planning input rather than a
            # presentation detail.
            page = decode_cursor(request.cursor, plan.limit or DEFAULT_PAGE_SIZE)

        with stage("federation", stats):
            result = await FederationEngine(
                self._registry.adapters(), deadline_ms=self._deadline_ms
            ).execute(
                plan,
                tenant_id=user.tenant_id,
                max_staleness_ms=request.max_staleness_ms,
                row_limit=page.probe_limit,
            )

        with stage("assemble", stats):
            envelope = await self._assembler.assemble(
                plan,
                result,
                tenant_id=user.tenant_id,
                trace_id=trace_id,
                max_staleness_ms=request.max_staleness_ms,
                page=page,
                stats=stats,
            )

        # `assemble_ms` cannot exist when the assembler copies `stats` — the
        # stage is still timing itself. Folded in once the timer has closed, so
        # the envelope reports all five stages rather than the four that happen
        # to finish before it. `stats` is the same dict the context managers
        # wrote to, so this also picks up anything a future stage adds.
        envelope.stats.update(stats)

        execution_ms = int((time.perf_counter() - started) * 1000)
        self._write_audit(parsed, entitled, user, envelope, trace_id, execution_ms)
        return envelope

    # -- the access trail --------------------------------------------------

    def _write_audit(
        self,
        parsed: Any,
        entitled: Any,
        user: UserContext,
        envelope: QueryEnvelope,
        trace_id: str,
        execution_ms: int,
    ) -> None:
        """One ``audit_logs`` row, with the literals stripped (ADR-033).

        Normalized from the **entitled** tree rather than the caller's text, so
        the trail records the query that actually ran — including the fact that
        an RLS predicate was applied — while still storing no literal values.
        """
        self._audit.write(
            AuditRecord(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                query_text=normalize_sql(entitled.ast),
                sources_accessed=parsed.connectors,
                rows_returned=len(envelope.rows),
                trace_id=trace_id,
                execution_ms=execution_ms,
            )
        )
