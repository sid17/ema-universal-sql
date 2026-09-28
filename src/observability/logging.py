"""Structured request logging, keyed by ``trace_id``.

**Why this exists.** A ``trace_id`` on its own is uninterpretable: the trace tells
you *where the time went*, but not what was asked or by whom. The pair is what
makes either useful — so every request emits one JSON line carrying the same
``trace_id`` the envelope returned and the span recorded.

This is the cheap half of the access trail. The durable half is the
``audit_logs`` table (``001_init.sql``), written after execution because it
needs ``sources_accessed`` and ``rows_returned``. Both join on ``trace_id``.

**What is deliberately not logged.** Never the token, and never a masked value: a
mask enforced in the response but leaked to the log is not a mask.
``query_text`` is normalised before auditing for the same reason — a literal like
``WHERE reporter_email = 'x@acme.com'`` puts in the log exactly the PII the CLS
rule strips from the result.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.config import get_settings
from src.observability.tracing import current_trace_id

ACCESS_LOGGER = "ema.access"

#: Routes that would otherwise dominate the log with no information value.
#: /metrics is scraped on a timer and /healthz is polled by compose.
_QUIET_PATHS = frozenset({"/metrics", "/healthz"})


class JsonFormatter(logging.Formatter):
    """Render a record as one JSON object per line.

    One line per event, so the log is greppable by ``trace_id`` and ingestible
    without a multi-line parser — the same reason the span exporter writes JSONL.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(getattr(record, "context", {}))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Install the JSON formatter on the root handler. Idempotent."""
    settings = get_settings()
    root = logging.getLogger()
    root.setLevel(settings.LOG_LEVEL.upper())

    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        handler.setFormatter(JsonFormatter())


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Emit one structured line per request.

    Identity is read from ``request.state`` *after* the handler runs, because it
    only exists once ``get_current_user`` has resolved it — an unauthenticated
    request logs with null identity, which is itself worth recording.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000

        if request.url.path in _QUIET_PATHS:
            return response

        user = getattr(request.state, "user", None)
        context = {
            # The join key: the same id the envelope returns and the span
            # records. Read from the ambient span rather than generated, so the
            # three artifacts genuinely correlate. Empty when nothing traced
            # this request, which is worth seeing rather than faking.
            "trace_id": current_trace_id() or None,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round(elapsed_ms, 2),
            "tenant_id": getattr(user, "tenant_id", None),
            "user_id": getattr(user, "user_id", None),
            # jti, so an access line names the exact credential used — without
            # logging the credential itself.
            "token_id": getattr(user, "token_id", None),
        }
        logging.getLogger(ACCESS_LOGGER).info("request", extra={"context": context})
        return response
