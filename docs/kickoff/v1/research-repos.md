# GitHub Research — v1 / Phase 0 (scaffold + contracts)

> **Scope:** deliberately a 15-minute confirmation pass, per `../../design/03-BUILD-PROCESS.md`
> step 1 — *"research is a 10–20 minute confirmation pass, not a survey"*, and **may not reopen a locked
> decision**. The four repos that settled the substrate (sqlglot, airbyte-python-cdk, universql,
> fastapi-permissions) were deep-read in a prior session; their cards live in
> `../../design/research/prototype-prior-art.md` and are **not**
> re-surveyed here.
>
> **Phase 0 had exactly one genuine open question**, created by `03-BUILD-PROCESS.md`'s decision to *front-load
> observability* — pulling the OTel span decorator and the Prometheus registry out of Phase 4 and into Phase 0 so
> later phases can decorate each stage as they write it. This document answers it and stops.

## Search keywords

`fastapi opentelemetry` · `opentelemetry python contrib` · `prometheus fastapi instrumentator` · `fastapi observability`

**Triage outcome:** the `fastapi opentelemetry` keyword returned only hobby repos (0–20 stars, e.g.
`naumnaum/fastapi-opentelemetry-tracing`, `mattbman23/template_fastapi`) — all below this skill's own
credibility bar and none carrying a pattern worth porting. The credible prior art for this question is the
canonical libraries themselves, so triage went directly to those.

## Repos analyzed

### open-telemetry/opentelemetry-python + opentelemetry-python-contrib (⭐ 2,649 / ⭐ 1,103)
- **What it does:** the OTel API/SDK for Python, plus `FastAPIInstrumentor`, the ASGI-middleware instrumentation
  that produces a server span per HTTP request.
- **Architecture:** `TracerProvider` → `SpanProcessor` → `SpanExporter`. Instrumentation is a separate package
  per framework, registered through an `opentelemetry_instrumentor` entry point.
- **Key patterns:**
  - **`FastAPIInstrumentor.instrument_app(app)` is per-app, not global** (source: `fastapi/__init__.py:290`).
    It guards on an `app._is_instrumented_by_opentelemetry` flag, so it is idempotent and safe to call inside an
    app factory — which is exactly the shape Phase 0 builds, and it means tests can construct isolated apps.
  - **`@tracer.start_as_current_span("name")` works as a plain function decorator** — runtime-verified, not
    inferred. The stage-span decorator Phase 0 owes Phase 2 is therefore a *thin wrapper* over a first-party
    API, not custom machinery.
  - **Nested spans auto-parent** off the ambient context, so the Phase 4 trace waterfall falls out of ordinary
    `with` blocks with no manual parent threading.
- **Trade-offs:** the instrumentation packages are pinned to `0.xxbN` pre-1.0 versions and must move in lockstep
  with each other; `fastapi ~= 0.92` is the declared floor. Resolved together they are consistent.
- **Relevance to us:** answers the whole open question. Also surfaces a **dependency gap in the Phase 0 spec** —
  see *Spec corrections* below.
- **Confidence:** High — read the instrumentor source and runtime-executed the span API.

### prometheus/client_python (⭐ 4,375)
- **What it does:** the reference Prometheus client — `Counter`/`Gauge`/`Histogram` against a
  `CollectorRegistry`, with `generate_latest()` rendering the text exposition format.
- **Key patterns:** a module-level default `REGISTRY` that every metric joins unless told otherwise;
  `generate_latest(REGISTRY)` is a pure function returning bytes, so the `/metrics` route is a two-liner we own
  rather than a mounted sub-app.
- **Relevance to us:** HLD §4 asks `/metrics` to carry **golden signals + a per-connector
  `rate_limit_remaining` gauge**. This library covers the gauge directly.
- **Confidence:** High — runtime-verified the gauge renders with both labels.

### trallnag/prometheus-fastapi-instrumentator (⭐ 1,487)
- **What it does:** wraps a FastAPI app in middleware that records the HTTP golden signals
  (`http_request_duration_seconds`, `http_requests_total`, request/response size) with a `handler` label.
- **Key patterns:**
  - **It defaults to `prometheus_client`'s shared `REGISTRY`** (`instrumentation.py:160-163`) — runtime-verified
    that a hand-declared `Gauge` lands on the same registry and appears in the same scrape. So golden signals
    and our domain metric need **one** `/metrics` endpoint, not two.
  - **`.instrument(app)` and `.expose(app)` are separable** — `instrument()` adds the middleware, `expose()` adds
    a route. We call `instrument()` only and keep our own route.
- **Trade-offs:** one extra dependency to avoid hand-writing ~40 lines of latency-histogram middleware. Worth it
  because HLD §4 explicitly asks for golden signals; not worth it if that line were dropped.
- **Relevance to us:** gets the golden-signal half of `/metrics` for two lines, leaving Phase 4 to do only the
  k6 run and the artifact capture.
- **Confidence:** High — read the registry and expose code paths, runtime-verified the shared-registry claim.

## Comparison table

| Dimension | opentelemetry-{sdk,instrumentation-fastapi} | prometheus/client_python | prometheus-fastapi-instrumentator |
|---|---|---|---|
| Answers | traces / the stage decorator | the domain gauge | the golden signals |
| Wiring cost | `instrument_app(app)` in the factory | declare metrics at module scope | `.instrument(app)` |
| Owns a route? | no | no (`generate_latest` is a function) | optional — **we decline it** |
| Registry | its own `TracerProvider` | default `REGISTRY` | **joins the default `REGISTRY`** |
| Verified how | source read + runtime probe | runtime probe | source read + runtime probe |
| Confidence | High | High | High |

## Runtime probe (the evidence)

Eight questions, executed against the pinned versions rather than read from docs — the Phase 0 AST spike had
just demonstrated that a docs-derived claim can be wrong in a way that fails silently.

| # | Question | Result |
|---|---|---|
| 1 | Does `start_as_current_span` work as a bare decorator? | ✅ yes |
| 2 | Do nested stage spans record in the right order? | ✅ `parse`, `connector_github`, `query` |
| 3 | Is `trace_id` readable **mid-request** (for the envelope)? | ✅ `format(ctx.trace_id, "032x")` → 32 hex chars |
| 4 | Do child spans share the root's `trace_id`? | ✅ one id across all spans |
| 5 | Is parent linkage automatic? | ✅ children point at the root's `span_id` |
| 6 | Are start/end times populated (for the waterfall)? | ✅ |
| 7 | Does a hand-declared `Gauge` land on the same `REGISTRY`? | ✅ |
| 8 | Does it render with labels? | ✅ `rate_limit_remaining{connector="github",tenant="tenant_acme"} 4870.0` |

Pinned versions probed: `opentelemetry-sdk 1.45.0`, `opentelemetry-instrumentation-fastapi 0.66b0`,
`prometheus-client 0.26.0`, `prometheus-fastapi-instrumentator 8.1.0`, `fastapi 0.141.1`, `starlette 1.7.0`.

## Patterns to adopt

- **`FastAPIInstrumentor.instrument_app(app)` inside the app factory**, not a global `instrument()` — from
  opentelemetry-python-contrib, because it is idempotent, per-app, and keeps tests isolated.
- **A `@stage_span("parse")` decorator that is a thin wrapper over `tracer.start_as_current_span`** — from the
  OTel API, because the first-party decorator already does the work; Phase 0 only adds the naming convention
  and the duration→`stats.connector_ms` capture that the envelope needs.
- **Read `trace_id` from the ambient span at envelope-assembly time** — from the probe, because it makes
  `QueryEnvelope.trace_id` free and genuinely resolvable against the exported trace, rather than a separate
  UUID that correlates with nothing.
- **One `/metrics` route that we own, calling `generate_latest(REGISTRY)`** — from prometheus/client_python,
  with `.instrument(app)`-without-`.expose(app)` from the instrumentator feeding the same registry.

## Patterns to skip

- **`.expose(app)` from prometheus-fastapi-instrumentator** — it would add a second `/metrics` route competing
  with the one Phase 0's spec already lists. Take the collectors, decline the route.
- **An OTLP exporter / Jaeger container in compose** — every hobby repo in the triage bolted one on. HLD §7
  keeps deployment surface minimal and Phase 4 needs *a readable waterfall*, not a tracing backend; a console
  or in-memory exporter satisfies the artifact. Revisit only if Phase 4 finds the screenshot unreadable without
  one. (Not a locked decision — flagged for `/architecture-extraction` to record either way.)
- **Auto-instrumenting Redis/psycopg** — available in contrib, but the graded trace is *connector time*
  (take-home line 161). Extra auto-spans would bury the signal the screenshot is supposed to show.

## Spec corrections this pass produced

1. **`pyproject.toml` dependency list is incomplete** (`phases/phase-0-scaffold.md` deliverable 3). It lists
   `opentelemetry-sdk` and `prometheus-client`, but `FastAPIInstrumentor` ships in a separate distribution.
   **Add `opentelemetry-instrumentation-fastapi`** (it pulls `-api`, `-asgi`, `-semantic-conventions`,
   `-util-http` transitively), and **`prometheus-fastapi-instrumentator`** if the golden signals in HLD §4 are
   to be met without hand-written middleware.
2. **No version conflict** between `fastapi 0.141` / `starlette 1.7` and instrumentation `0.66b0` — the
   declared floor is `fastapi ~= 0.92`. Worth recording because the instrumentation packages are pre-1.0 and
   must be upgraded as a set.

## Open questions remaining

**None for Phase 0.** The observability seam is fully answered above; every other Phase 0 deliverable
(app factory, HS256 JWT, Pydantic contract models, psycopg migration runner, TTL-cached control-plane
repository) is well-trodden work against a locked stack with no prior art worth citing.

## Next step

→ Step 2: `/architecture-extraction` — formalize `01-EXECUTION-PLAN.md` §A/§B as Accepted ADRs, and record the
two decisions this pass *did* open: the `/metrics` single-route choice and the no-tracing-backend choice.
