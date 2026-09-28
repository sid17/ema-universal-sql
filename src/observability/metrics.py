"""Prometheus metrics: the golden-signal collectors plus the three we own.

ADR-015: there is exactly **one** `/metrics` route and `src/gateway/routes.py`
owns it. We take the instrumentator's collectors (`.instrument(app)`) and
decline its route (`.expose(app)`), which would otherwise register a second,
competing `/metrics`. Both metric families land in `prometheus_client`'s shared
`REGISTRY`, so one `generate_latest(REGISTRY)` scrape carries all of them.

**Phase 4 adds the two histograms the phase file names and never had** — the
golden signals the instrumentator supplies are per-*route*, which cannot answer
"how long did Jira take". It also adds the three `observe_*` / `set_*` helpers,
so no call site outside this module imports `prometheus_client` or reaches for a
registry global: one module owns the registry, and the rest of the codebase
records numbers through named functions.

**Multiprocess mode (Phase 5).** The default is now 8 uvicorn workers, and
`prometheus_client`'s `REGISTRY` is **per process** — so a scrape would report
whichever worker happened to answer, silently dividing every counter by eight.
When `PROMETHEUS_MULTIPROC_DIR` is set, each worker writes its samples to that
directory and `render_metrics` builds a **fresh** `CollectorRegistry` per scrape
with a `MultiProcessCollector` over it. ADR-015 still holds — `/metrics` is one
route we own — but the registry it renders from changes. Two consequences worth
naming: a `Gauge` needs an explicit `multiprocess_mode`, and the `process_*` /
`python_gc_*` collectors do not work in this mode at all.

A note on label names. The phase file asks for `http_requests_total{route,code}`;
the instrumentator spells them `{handler,method,status}`. We keep its spelling
(ADR-040) — matching ours would mean replacing the library's default collectors
to rename two labels, which buys no behaviour and moves the golden signals onto
a code path nothing else exercises. The phase file is corrected instead.
"""

from __future__ import annotations

import os
from typing import TypeVar

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)
from prometheus_client.metrics import MetricWrapperBase
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_fastapi_instrumentator import metrics as fastapi_metrics
from starlette.applications import Starlette

RATE_LIMIT_REMAINING_NAME = "rate_limit_remaining"
RATE_LIMIT_REMAINING_LABELS = ("connector", "tenant")

QUERY_DURATION_NAME = "query_duration_seconds"
CONNECTOR_FETCH_DURATION_NAME = "connector_fetch_duration_seconds"

#: Set by the container when running more than one worker. Its presence is
#: what switches `render_metrics` to the multiprocess path — the same signal
#: `prometheus_client` itself keys off, so there is no second source of truth.
MULTIPROC_DIR_ENV = "PROMETHEUS_MULTIPROC_DIR"

M = TypeVar("M", bound=MetricWrapperBase)


def multiprocess_enabled() -> bool:
    """True when workers are writing samples to a shared directory."""
    return bool(os.environ.get(MULTIPROC_DIR_ENV))


def _get_or_create(
    metric: type[M],
    name: str,
    doc: str,
    labels: tuple[str, ...] = (),
    **kwargs: object,
) -> M:
    """Declare a collector, tolerating a second import of this module.

    `prometheus_client` raises `ValueError` on a duplicate timeseries and offers
    no public lookup for an already-registered collector, so the recovery path
    reads the registry's index directly. Verified against the library: a
    `Histogram` registers its base name as well as the `_bucket`/`_count`/`_sum`
    suffixes, so the base name is a valid key for both metric types. A
    `ValueError` that is *not* a duplicate propagates (LAW 4).
    """
    try:
        return metric(name, doc, labels, registry=REGISTRY, **kwargs)
    except ValueError:
        existing = REGISTRY._names_to_collectors.get(name)
        if not isinstance(existing, metric):
            raise
        return existing


#: Per-connector, per-tenant budget left. Fed from `ResultAssembler._budgets`,
#: which is the one place already asking the bucket for this number (ADR-043).
#:
#: `multiprocess_mode="livemostrecent"`, and the mode is a **correctness**
#: decision rather than a formatting one.
#:
#: This gauge does not measure a per-worker quantity that wants aggregating. It
#: measures ONE shared number — a token bucket in Redis — sampled by whichever
#: worker happened to serve a query. Eight workers are eight observers of the
#: same thing, so the only reading that is ever current is the newest.
#:
#: It was `livemin` until this was measured. The argument for it was that the
#: smallest live reading is the conservative answer; that holds for a quantity
#: each worker owns a *share* of, and this is not one. What it did instead was
#: pin the gauge to a stale low sample — a worker that served a query during an
#: earlier drain kept voting `0` long after the bucket refilled, so an alert on
#: "budget exhausted" fires for a tenant who is fine. It also made the gauge and
#: `QueryEnvelope.rate_limit_status` report different numbers for the same
#: bucket while both looked authoritative, which is worse than having no metric
#: at all (ADR-043 exists to prevent exactly that). See
#: `tests/unit/test_metrics_multiprocess.py`.
#:
#: `live*` is still wanted: it drops the readings of workers that have exited,
#: so a restarted worker leaves no stale budget behind.
rate_limit_remaining = _get_or_create(
    Gauge,
    RATE_LIMIT_REMAINING_NAME,
    "Requests left in the tenant's token bucket for a connector.",
    RATE_LIMIT_REMAINING_LABELS,
    multiprocess_mode="livemostrecent",
)

#: End-to-end query latency — the histogram P50/P95 are read off.
#:
#: Observed in the **route**, not the runner, and in a `finally` (ADR-040). A
#: histogram that only sees successes reports a P95 better than the one users
#: experience, and entitlement denials and 400s never reach the runner at all.
query_duration_seconds = _get_or_create(
    Histogram,
    QUERY_DURATION_NAME,
    "End-to-end POST /v1/query latency, including requests that failed.",
)

#: Per-connector fetch latency. The third view of `SourceFetch.elapsed_ms` — the
#: span (ADR-037) and the envelope's `stats.connector_ms` are the other two, and
#: all three read the same `perf_counter` pair so they cannot disagree.
connector_fetch_duration_seconds = _get_or_create(
    Histogram,
    CONNECTOR_FETCH_DURATION_NAME,
    "Time one connector took to answer, including cache hits and failures.",
    ("connector",),
)


def observe_query(seconds: float) -> None:
    """Record one end-to-end query, successful or not."""
    query_duration_seconds.observe(seconds)


def observe_connector_fetch(connector: str, seconds: float) -> None:
    """Record one connector fetch. Called on every exit path, timeouts included."""
    connector_fetch_duration_seconds.labels(connector=connector).observe(seconds)


def set_rate_limit_remaining(tenant: str, connector: str, remaining: int) -> None:
    """Publish the tokens left for one `(tenant, connector)` bucket."""
    rate_limit_remaining.labels(connector=connector, tenant=tenant).set(remaining)


_INSTRUMENTATOR: Instrumentator | None = None


def _instrumentator() -> Instrumentator:
    """The single process-wide instrumentator, with its collectors built once.

    The golden-signal collectors are built here rather than left to the
    middleware's lazy default: the middleware builds them per app, and its
    duplicate handling returns `None`, which would leave a second app in the
    same process instrumented but recording nothing.
    """
    global _INSTRUMENTATOR
    if _INSTRUMENTATOR is None:
        instrumentation = fastapi_metrics.default(registry=REGISTRY)
        if instrumentation is None:
            raise RuntimeError(
                "Golden-signal collectors are already registered on the default "
                "REGISTRY by another instance of this module — /metrics would "
                "silently record nothing for this app."
            )
        instrumentator = Instrumentator(registry=REGISTRY)
        instrumentator.add(instrumentation)
        _INSTRUMENTATOR = instrumentator
    return _INSTRUMENTATOR


def instrument_app(app: Starlette) -> Starlette:
    """Attach the golden-signal middleware. Deliberately **not** `.expose(app)`."""
    _instrumentator().instrument(app)
    return app


def render_metrics() -> tuple[bytes, str]:
    """`(body, content_type)` for the `/metrics` route's `Response`.

    Single worker: renders the process registry, exactly as before. Multiple
    workers: builds a **new** registry per scrape and collects every worker's
    samples into it. New each time on purpose — `MultiProcessCollector` reads
    the directory at collection time, and a registry reused across scrapes would
    accumulate a collector per call and report each sample repeatedly.
    """
    if not multiprocess_enabled():
        return generate_latest(REGISTRY), CONTENT_TYPE_LATEST

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    return generate_latest(registry), CONTENT_TYPE_LATEST
