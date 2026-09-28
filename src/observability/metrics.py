"""Prometheus metrics: the golden-signal collectors plus our own gauge.

ADR-015: there is exactly **one** `/metrics` route and `src/gateway/routes.py`
owns it. We take the instrumentator's collectors (`.instrument(app)`) and
decline its route (`.expose(app)`), which would otherwise register a second,
competing `/metrics`. Both metric families land in `prometheus_client`'s shared
`REGISTRY`, so one `generate_latest(REGISTRY)` scrape carries all of them.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Gauge, generate_latest
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_fastapi_instrumentator import metrics as fastapi_metrics
from starlette.applications import Starlette

RATE_LIMIT_REMAINING_NAME = "rate_limit_remaining"
RATE_LIMIT_REMAINING_LABELS = ("connector", "tenant")


def _get_or_create_gauge() -> Gauge:
    """Declare the gauge, tolerating a second import of this module.

    `prometheus_client` raises `ValueError` on a duplicate timeseries and offers
    no public lookup for an already-registered collector, so the recovery path
    reads the registry's index directly. A `ValueError` that is *not* a
    duplicate propagates (LAW 4).
    """
    try:
        return Gauge(
            RATE_LIMIT_REMAINING_NAME,
            "Requests left in the tenant's token bucket for a connector.",
            RATE_LIMIT_REMAINING_LABELS,
            registry=REGISTRY,
        )
    except ValueError:
        existing = REGISTRY._names_to_collectors.get(RATE_LIMIT_REMAINING_NAME)
        if not isinstance(existing, Gauge):
            raise
        return existing


#: Per-connector, per-tenant budget left. Phase 1's token bucket sets it.
rate_limit_remaining = _get_or_create_gauge()

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
    """`(body, content_type)` for the `/metrics` route's `Response`."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
