"""The compliance access trail: one row per query (design-doc §3.4).

**``query_text`` stores the NORMALIZED SQL, with every literal replaced by
``?``** (ADR-033). This is the whole reason the module is more than an INSERT.

The canonical CLS rule exists to stop ``reporter_email`` ever reaching a caller.
But a query reading ``WHERE issue.reporter_email = 'dana@acme.com'`` would, with
the obvious implementation, write that exact address into ``audit_logs`` in
plaintext and keep it forever. The masking layer would be defeated by the
logging layer — and worse, it would be defeated *silently*, in a table nobody
reads until an audit.

Normalization runs over the **AST**, not over the string. A regex across SQL text
is precisely the string-level reasoning this design replaced with a parse tree:
it would miss escaped quotes, numeric literals and anything the caller spelled
unusually. sqlglot already holds the tree; replacing its ``Literal`` nodes is
exact by construction.

What is kept is what an access trail is actually for: **who** asked, **when**,
**which sources** were touched, **how many rows** came back, and the
``trace_id`` that ties the row to the spans. The literal values are the one part
with no audit purpose and a real disclosure cost.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlglot import exp

logger = logging.getLogger(__name__)

#: What a literal becomes. The SQL placeholder, so a normalized statement still
#: reads as a statement and groups naturally with others of the same shape.
PLACEHOLDER = "?"


def normalize_sql(tree: exp.Expression) -> str:
    """Render ``tree`` with every literal replaced by ``?``.

    Works on a copy: the caller's tree is still being executed, and a rewrite in
    place would swap the real predicates out from under the query.
    """
    redacted = tree.copy()
    for literal in redacted.find_all(exp.Literal):
        literal.replace(exp.column(PLACEHOLDER))
    return redacted.sql(dialect="duckdb")


@dataclass(frozen=True)
class AuditRecord:
    """One access-trail entry."""

    tenant_id: str
    user_id: str
    query_text: str
    sources_accessed: tuple[str, ...]
    rows_returned: int
    trace_id: str
    execution_ms: int


class AuditLogger:
    """Writes one ``audit_logs`` row per query."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def write(self, record: AuditRecord) -> None:
        """Insert the record.

        **A failed write is logged, not raised.** This is the one deliberate
        exception to LAW 4's "log or throw" in this codebase, and the reasoning
        is worth stating: the query has already succeeded and the caller's rows
        are already correct and entitled. Failing the response because the audit
        INSERT failed would turn a logging outage into a customer-facing outage,
        and callers would retry — producing more load on the thing that is
        already broken.

        It is emphatically **not** swallowed: it is logged at ERROR with the
        traceback and the full context, so the gap is visible to whoever watches
        the logs. A compliance posture that requires the write to be
        transactional with the read would invert this trade-off, and would need a
        durable queue rather than a synchronous INSERT — noted in the README as
        a prototype limitation rather than pretended away.
        """
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    "INSERT INTO audit_logs (tenant_id, user_id, query_text, "
                    "  sources_accessed, rows_returned, trace_id, execution_ms) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        record.tenant_id,
                        record.user_id,
                        record.query_text,
                        list(record.sources_accessed),
                        record.rows_returned,
                        record.trace_id,
                        record.execution_ms,
                    ),
                )
        except Exception:
            logger.error(
                "audit log write failed; the query succeeded but left no access trail",
                exc_info=True,
                extra={
                    "context": {
                        "tenant_id": record.tenant_id,
                        "user_id": record.user_id,
                        "trace_id": record.trace_id,
                    }
                },
            )
