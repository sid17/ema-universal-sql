"""OpenTelemetry tracer provider and the `@stage_span` decorator.

ADR-016: there is **no** OTLP exporter and no tracing backend in `docker-compose`
— the spans carry parents and durations, which is all the Phase 4 waterfall needs.
The sink is chosen by ``OTEL_EXPORTER``: ``file`` (JSONL, the waterfall artifact),
``console`` (local debugging) or ``none`` (the k6 run). Tests bind an in-memory one.

`stage_span` is deliberately a *thin* wrapper over `tracer.start_as_current_span`
(runtime-verified: the first-party API already works as a bare decorator,
auto-parents nested spans and records start/end times). What this module adds is
(a) the stage naming convention and (b) the elapsed-ms attribute that Phase 2
reads into `QueryEnvelope.stats.connector_ms`.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, TypeVar

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)

from src.config import get_settings

logger = logging.getLogger(__name__)

TRACER_NAME = "ema.universal-sql"

#: Span attribute carrying the wall-clock duration of a stage, in milliseconds.
ELAPSED_MS_ATTRIBUTE = "stage.elapsed_ms"

#: What :func:`current_trace_id` returns when there is no recording span in
#: scope. The all-zero id that OTel's INVALID_SPAN reports is a *valid-looking*
#: 32-hex string that resolves to nothing, so it is never returned; callers get
#: this falsy sentinel and can tell "not traced" from "traced".
NO_ACTIVE_TRACE = ""

F = TypeVar("F", bound=Callable[..., Any])

_PROVIDER: TracerProvider | None = None

#: Open handle for the "file" sink, kept so a reset can close it. The exporter
#: itself never closes what it was handed (`ConsoleSpanExporter.shutdown` is a
#: no-op), so leaking this would leak a file descriptor per reset.
_TRACE_FILE: IO[str] | None = None


def _as_jsonl(span: ReadableSpan) -> str:
    """One span, one line — `to_json()` pretty-prints across lines by default.

    Phase 4 greps this file by `trace_id`, so a multi-line record would break
    every line-oriented tool pointed at it.
    """
    return span.to_json(indent=None) + "\n"


def _open_trace_file(path: str) -> IO[str] | None:
    """Open `path` for appending, creating its parent. None if that failed.

    LAW 4: a bad path must not vanish silently, and must not take the whole app
    down either — tracing is diagnostics. The caller logs the fallback.
    """
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target.open("a", encoding="utf-8")
    except OSError:
        logger.error("tracing: cannot open OTEL_TRACE_FILE %r", path, exc_info=True)
        return None


def _configured_processor() -> SpanProcessor | None:
    """The production span processor for the current `OTEL_EXPORTER` setting.

    Returns None for "none": the k6 run must pay *zero* export cost, and a
    no-op exporter behind a processor still serializes every span.
    """
    global _TRACE_FILE
    mode = get_settings().OTEL_EXPORTER
    if mode == "none":
        return None

    exporter = ConsoleSpanExporter()
    if mode == "file":
        path = get_settings().OTEL_TRACE_FILE
        _TRACE_FILE = _open_trace_file(path)
        if _TRACE_FILE is None:
            logger.warning("tracing: falling back to console exporter (wanted %r)", path)
        else:
            exporter = ConsoleSpanExporter(out=_TRACE_FILE, formatter=_as_jsonl)

    # Batch, never Simple: `SimpleSpanProcessor` exports on the request thread,
    # so the export cost would land inside the latency Phase 4 measures.
    return BatchSpanProcessor(exporter)


def _close_trace_file() -> None:
    global _TRACE_FILE
    if _TRACE_FILE is not None:
        _TRACE_FILE.close()
        _TRACE_FILE = None


def _build_provider(exporter: SpanExporter | None) -> TracerProvider:
    """Provider for `exporter`, or for whatever `OTEL_EXPORTER` selects.

    An explicit `exporter` (tests) gets a `SimpleSpanProcessor`: it exports
    inline, so an assertion right after the call sees the span without a flush.
    That is exactly the property that makes it wrong in production — see
    `_configured_processor`.
    """
    provider = TracerProvider()
    if exporter is not None:
        processor: SpanProcessor | None = SimpleSpanProcessor(exporter)
    else:
        processor = _configured_processor()
    if processor is not None:
        provider.add_span_processor(processor)
    return provider


def configure_tracing(exporter: SpanExporter | None = None) -> TracerProvider:
    """Build the process tracer provider once and return it.

    Idempotent: the FastAPI app factory may run more than once in a test
    session, and re-running it must not attach a second span processor (which
    would export every span twice). The first call wins; later calls ignore
    their `exporter` argument and return the provider already in place.
    """
    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = _build_provider(exporter)
        # Best effort: the OTel global can only be set once per process. We keep
        # our own reference so `get_tracer()` is correct either way.
        trace.set_tracer_provider(_PROVIDER)
    return _PROVIDER


def reset_tracing(exporter: SpanExporter | None = None) -> TracerProvider:
    """Replace the provider with one exporting to `exporter`. Test-only seam.

    `configure_tracing` is first-call-wins, so a test that needs its spans in an
    `InMemorySpanExporter` cannot rely on being the first caller in the session.
    This gives it a deterministic binding regardless of import order. Passing no
    exporter rebuilds from `OTEL_EXPORTER`, which is how the sink-selection
    tests exercise the production path.
    """
    global _PROVIDER
    if _PROVIDER is not None:
        _PROVIDER.shutdown()  # flushes pending batches before the file closes
    _close_trace_file()
    _PROVIDER = _build_provider(exporter)
    return _PROVIDER


def get_tracer() -> trace.Tracer:
    """The tracer every stage span is started from."""
    return configure_tracing().get_tracer(TRACER_NAME)


def _record_elapsed(span: trace.Span, started: float) -> None:
    span.set_attribute(ELAPSED_MS_ATTRIBUTE, (time.perf_counter() - started) * 1000.0)


def stage_span(name: str) -> Callable[[F], F]:
    """Wrap a pipeline stage in a span named `name`, recording its elapsed ms.

    Works on both sync and async callables — Phase 2's connector calls are
    coroutines, and wrapping one with a sync wrapper would close the span before
    the await ever ran, timing the coroutine's *creation* instead of its work.
    """

    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with get_tracer().start_as_current_span(name) as span:
                    started = time.perf_counter()
                    try:
                        return await func(*args, **kwargs)
                    finally:
                        _record_elapsed(span, started)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with get_tracer().start_as_current_span(name) as span:
                started = time.perf_counter()
                try:
                    return func(*args, **kwargs)
                finally:
                    _record_elapsed(span, started)

        return sync_wrapper  # type: ignore[return-value]

    return decorator


def current_trace_id() -> str:
    """The ambient span's trace id as 32 lowercase hex chars.

    Read from the *active* span rather than generated, so the envelope's
    `trace_id` actually resolves against the exported trace instead of being a
    decorative UUID. Returns :data:`NO_ACTIVE_TRACE` (the empty string) outside
    any span — see that constant for why the all-zero id is not returned.
    """
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return NO_ACTIVE_TRACE
    return format(ctx.trace_id, "032x")
