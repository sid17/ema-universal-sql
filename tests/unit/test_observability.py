"""Unit tests for the observability seam. Infra-free: no Docker, no network."""

from __future__ import annotations

import io
import json
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.config import get_settings
from src.observability.tracing import (
    ELAPSED_MS_ATTRIBUTE,
    NO_ACTIVE_TRACE,
    current_trace_id,
    reset_tracing,
    stage_span,
)

TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


@pytest.fixture
def spans() -> InMemorySpanExporter:
    """Bind the tracer provider to an in-memory exporter for this test."""
    exporter = InMemorySpanExporter()
    reset_tracing(exporter)
    return exporter


def processors_of(provider: TracerProvider) -> tuple[SpanProcessor, ...]:
    """The processors actually attached to `provider`.

    Private OTel attribute on purpose: "no processor at all" is the whole point
    of the `none` sink, and only the provider's own list can prove it.
    """
    return tuple(provider._active_span_processor._span_processors)


@pytest.fixture
def trace_file(tmp_path: Path) -> Path:
    """Where the `file` sink is pointed. Under a missing parent, on purpose."""
    return tmp_path / "nested" / "spans.jsonl"


@pytest.fixture
def sink(
    monkeypatch: pytest.MonkeyPatch, trace_file: Path
) -> Iterator[Callable[[str], TracerProvider]]:
    """Rebuild the provider from `OTEL_EXPORTER`, the way production does.

    Yields a factory: `sink(mode)` returns the provider. The settings cache is
    cleared on both sides so neither this test nor its neighbours see a stale
    `Settings`, and the provider is torn down so the trace file is closed.
    """

    def configure(mode: str) -> TracerProvider:
        monkeypatch.setenv("OTEL_EXPORTER", mode)
        monkeypatch.setenv("OTEL_TRACE_FILE", str(trace_file))
        get_settings.cache_clear()
        return reset_tracing()

    yield configure
    reset_tracing(InMemorySpanExporter())
    get_settings.cache_clear()


def test_stage_span_records_a_named_span(spans: InMemorySpanExporter) -> None:
    @stage_span("parse")
    def parse() -> str:
        return "parsed"

    assert parse() == "parsed"

    finished = spans.get_finished_spans()
    assert [span.name for span in finished] == ["parse"]
    assert finished[0].attributes[ELAPSED_MS_ATTRIBUTE] >= 0.0


async def test_stage_span_wraps_an_async_function(spans: InMemorySpanExporter) -> None:
    @stage_span("connector_github")
    async def fetch() -> list[int]:
        return [1, 2, 3]

    assert await fetch() == [1, 2, 3]

    finished = spans.get_finished_spans()
    assert [span.name for span in finished] == ["connector_github"]
    assert finished[0].end_time > finished[0].start_time


async def test_nested_spans_share_a_trace_and_link_to_the_parent(
    spans: InMemorySpanExporter,
) -> None:
    @stage_span("connector_jira")
    async def child() -> str:
        return current_trace_id()

    @stage_span("query")
    async def parent() -> tuple[str, str]:
        return current_trace_id(), await child()

    parent_trace_id, child_trace_id = await parent()

    by_name = {span.name: span for span in spans.get_finished_spans()}
    assert set(by_name) == {"query", "connector_jira"}
    assert by_name["query"].context.trace_id == by_name["connector_jira"].context.trace_id
    assert by_name["connector_jira"].parent.span_id == by_name["query"].context.span_id
    assert parent_trace_id == child_trace_id


def test_current_trace_id_is_32_hex_chars_inside_a_span(spans: InMemorySpanExporter) -> None:
    @stage_span("plan")
    def plan() -> str:
        return current_trace_id()

    trace_id = plan()
    assert TRACE_ID_RE.match(trace_id)
    assert trace_id == format(spans.get_finished_spans()[0].context.trace_id, "032x")


def test_current_trace_id_outside_any_span_is_the_documented_sentinel(
    spans: InMemorySpanExporter,
) -> None:
    trace_id = current_trace_id()
    assert trace_id == NO_ACTIVE_TRACE == ""
    assert trace_id != "0" * 32  # the invalid all-zero id is never handed out
    assert not spans.get_finished_spans()


def test_file_sink_writes_one_parseable_json_object_per_span(
    sink: Callable[[str], TracerProvider], trace_file: Path
) -> None:
    """The Phase 4 artifact contract: JSONL, not pretty-printed JSON.

    Each line is parsed *independently*, which is the only assertion that
    actually rules out `to_json()`'s default multi-line output.
    """
    provider = sink("file")

    @stage_span("plan")
    def plan() -> None:
        return None

    for _ in range(3):
        plan()
    provider.force_flush()

    lines = trace_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["name"] for line in lines] == ["plan"] * 3


def test_file_sink_records_the_trace_id_the_envelope_reports(
    sink: Callable[[str], TracerProvider], trace_file: Path
) -> None:
    """What makes the waterfall correlatable with the audit trail.

    `to_json()` renders the id as `0x`-prefixed hex; `current_trace_id()` is the
    bare 32 hex chars, so the artifact is greppable by the envelope's value.
    """
    provider = sink("file")

    @stage_span("query")
    def query() -> str:
        return current_trace_id()

    trace_id = query()
    provider.force_flush()

    exported = json.loads(trace_file.read_text(encoding="utf-8").strip())
    assert TRACE_ID_RE.match(trace_id)
    assert exported["context"]["trace_id"] == f"0x{trace_id}"
    assert trace_id in trace_file.read_text(encoding="utf-8")


def test_file_sink_creates_the_parent_directory(
    sink: Callable[[str], TracerProvider], trace_file: Path
) -> None:
    assert not trace_file.parent.exists()
    sink("file")
    assert trace_file.parent.is_dir()


def test_production_sinks_export_off_the_request_thread(
    sink: Callable[[str], TracerProvider],
) -> None:
    """`SimpleSpanProcessor` would put export cost inside the measured latency."""
    for mode in ("file", "console"):
        (processor,) = processors_of(sink(mode))
        assert isinstance(processor, BatchSpanProcessor)


def test_none_sink_attaches_no_processor_and_writes_nothing(
    sink: Callable[[str], TracerProvider], trace_file: Path
) -> None:
    """Zero export cost for the k6 run — not a no-op exporter still serializing."""
    provider = sink("none")
    assert processors_of(provider) == ()

    @stage_span("plan")
    def plan() -> None:
        return None

    plan()
    provider.force_flush()

    assert not trace_file.exists()


def drain_console_sink(provider: TracerProvider) -> str:
    """Emit a span through `provider` and return what its exporter wrote.

    The exporter's stream is swapped for a buffer instead of capturing stdout:
    `ConsoleSpanExporter` binds `sys.stdout` at import time, which pytest's
    capture layers replace afterwards, so `capsys`/`capfd` never see the writes.
    Asserting `out is sys.stdout` first is what keeps the swap honest.
    """
    (processor,) = processors_of(provider)
    exporter = processor.span_exporter
    assert isinstance(exporter, ConsoleSpanExporter)
    assert exporter.out is sys.stdout

    buffer = io.StringIO()
    exporter.out = buffer

    @stage_span("connector_github")
    def fetch() -> None:
        return None

    fetch()
    provider.force_flush()
    return buffer.getvalue()


def test_console_sink_writes_spans_to_stdout(
    sink: Callable[[str], TracerProvider], trace_file: Path
) -> None:
    written = drain_console_sink(sink("console"))

    assert json.loads(written)["name"] == "connector_github"
    assert not trace_file.exists()


def test_unreadable_trace_path_falls_back_to_console_instead_of_crashing(
    sink: Callable[[str], TracerProvider],
    trace_file: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LAW 4: the failure is loud in the log, and the app still exports."""
    trace_file.parent.write_text("not a directory", encoding="utf-8")

    written = drain_console_sink(sink("file"))

    assert "cannot open OTEL_TRACE_FILE" in caplog.text
    assert "falling back to console" in caplog.text
    assert json.loads(written)["name"] == "connector_github"
