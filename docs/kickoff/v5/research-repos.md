# Phase 4 research — observability, load & artifacts

> **Scope:** `03-BUILD-PROCESS.md` caps this step at a 10–20 minute confirmation pass and names Phase 4 as
> "the only phase with a real open question (OTel FastAPI instrumentation shape, k6 script structure)."
> Half of that is already answered — Phase 0 built the OTel wiring and v1 research settled it (ADR-015/016,
> amended by ADR-018). So **no repositories were surveyed for this phase.** The instrumentation question is
> closed; the k6 question is a *runtime* question, not a prior-art one.
>
> What follows is therefore the same thing v3 did: **runtime probes against the pinned versions**, recorded
> with their measured output. Six findings. Four of them are gaps between what a locked document claims and
> what the code actually does — which is exactly what a probe pass is for.

---

## Why no repo survey

The four deep-read cards in `01-EXECUTION-PLAN.md` §D cover the pipeline, not the harness. For this phase the
two candidate survey targets would have been "how do people structure a k6 script" and "how do people render
an OTel trace without a backend". Both are answered better by running the tools than by reading someone else's
repository:

- k6's script contract is a documented API with one correct shape per executor. A survey would return
  stylistic variation, not a decision.
- Rendering a waterfall from our own JSONL is ~80 lines against a format we control. There is no prior art to
  borrow that is smaller than the thing itself.

Writing one line saying so is an explicitly valid outcome of this step (`03-BUILD-PROCESS.md` §1). This is
that line, with the probe results that replace it.

---

## F1 — k6 v2.3.0: `constant-arrival-rate` + `handleSummary` work, with **no network dependency**

The phase file requires `make load` to work on a fresh clone without the reviewer installing k6, and requires
the summary to land in `docs/k6-summary.txt`. Both hinge on details worth measuring rather than assuming.

```
$ docker run --rm grafana/k6:latest version
k6 v2.3.0 (commit/e088784614, go1.27.1, linux/arm64)
```

A 3-second probe with a `constant-arrival-rate` scenario and a `handleSummary` that returns a **mounted host
path** as a key:

```
     scenarios: (100.00%) 1 scenario, 50 max VUs, 33s max duration
              * probe: 50.00 iterations/s for 3s (maxVUs: 10-50, gracefulStop: 30s)
probe ✓ [ 100% ] 00/10 VUs  3s  50.00 iters/s

$ cat summary.txt           # written into the bind mount by handleSummary
metrics: checks,data_received,data_sent,iteration_duration,iterations,vus,vus_max
iteration_duration values: {"min":0.019583,"med":0.070708,"max":0.35675,
                            "p(90)":0.131,"p(95)":0.14825,"avg":0.07948514569536423}
root_group? true state? {"isStdOutTTY":false,"isStdErrTTY":false,"testRunDurationMs":3002.019945}
```

**Three things this settles.**

1. `handleSummary` returning `{'/out/summary.txt': text}` writes through a bind mount. So `docs/k6-summary.txt`
   is produced *by the run*, not by shell-redirecting stdout — which matters because stdout carries the live
   progress bar and ANSI escapes.
2. `data.metrics.<name>.values` already contains `min / med / max / avg / p(90) / p(95)`. **We render the
   summary ourselves in plain JS.** The conventional move is
   `import { textSummary } from 'https://jslib.k6.io/...'` — a *remote import*, which would make `make load`
   fail on a machine with no network and silently couple a submission artifact to a CDN. Rejected on those
   grounds; see ADR-039.
3. `data.state.testRunDurationMs` gives the real wall-clock denominator, so the achieved RPS in the summary is
   measured rather than restated from the requested `rate`.

**Consequence for the script:** zero imports beyond `k6/http` and `k6`. No jslib, no network, no k6 extension.

---

## F2 — the span log reassembles into a tree; the format is stable

ADR-018 exports spans as one JSON object per line. Phase 4 is the first consumer, so "can a waterfall actually
be rebuilt from this?" had to be answered before committing to a renderer.

```
$ python - <<'PY'   # over traces/spans.jsonl, 34 MB accumulated across sessions
distinct span names: 27
   4024  POST /v1/query http send
   1342  POST /v1/query
   1135  parse
   1082  gateway
    970  entitlement
    918  plan
    893  federation
    865  assemble
traces: 9554
PY
```

Every record carries `context.trace_id`, `context.span_id`, `parent_id`, `start_time`, `end_time` and
`attributes["stage.elapsed_ms"]`. Ids are `0x`-prefixed hex; timestamps are ISO-8601 with `Z`. That is
sufficient to group by `trace_id`, link child→parent, and lay spans out on a shared time axis —
**no backend required, which is what ADR-016 promised and this is the first evidence for it.**

**Also measured:** the per-name counts are uneven (1135 `parse` vs 1082 `gateway`) because
`BatchSpanProcessor` drops whatever is still queued when the process exits. The renderer must therefore
**select a trace that is complete**, not merely the most recent one — a half-flushed trace would render a
waterfall with holes in it and look like a bug in the engine.

---

## F3 — GAP: there are **no per-connector spans and no join span**

The phase file's waterfall is specified as seven spans: `gateway_ms`, `parse_ms`, `entitlement_ms`, `plan_ms`,
`connector_github_ms`, `connector_jira_ms`, `duckdb_join_ms`.

```
$ grep -n "stage_span\|start_as_current_span\|get_tracer" src/execution/*.py src/connectors/*.py
(no matches)
```

**Five exist. The two that carry the phase's entire punchline do not.** `src/pipeline/runner.py` spans the five
stages; nothing inside `FederationEngine` spans the individual fetches or the DuckDB join. The "Done when"
sentence — *"reads the trace to see 'P95 was Jira not the engine'"* — is unprovable against the current span
set, because the trace bottoms out at one `federation` span that contains both connectors **and** the join.

Note this is a *coverage* gap, not a design failure: the envelope already reports
`stats.connector_ms = {github: …, jira: …}`, so the timing exists — it just never reaches a span. Phase 4
closes it inside `federation.py` (ADR-037).

---

## F4 — GAP: the mock connectors have **no simulated latency**, contradicting a locked document

```
$ grep -rn "asyncio.sleep\|latency" src/connectors/
(no matches)

$ curl -s .../v1/query ... | jq .stats
{
  "parse_ms": 1.51, "entitlement_ms": 0.06, "plan_ms": 0.12,
  "federation_ms": 15.51,
  "connector_ms": { "github": 2.05, "jira": 2.05 },
  "rows_examined": 3, "assemble_ms": 0.59
}
```

Two locked statements assume otherwise:

- **HLD non-goals table, line 39:** *"Mock connectors with deterministic datasets + simulated
  pagination/latency/429."* Pagination is built. The 429 is built. **Latency was not.**
- **`phase-4-observability.md` itself:** *"sending the canonical query with a high `max_staleness_ms` so cache
  hits dominate (the run should not be measuring the mock's simulated latency)"* — a sentence that presupposes
  the latency exists.

**Why this matters more than it looks.** With both sources answering in 2.05 ms, the waterfall's honest reading
is *"the connectors are free and DuckDB is the cost"* — the precise inverse of the claim the artifact is
supposed to support, and the inverse of how a real federated query behaves, where the remote call dominates by
two orders of magnitude. Shipping that trace would be an artifact that argues against its own design doc.

This is a Phase 1 gap surfacing in Phase 4, and the fix belongs to whichever phase notices (ADR-038). It is
*closing* a locked claim, not new scope.

---

## F5 — GAP: `rate_limit_remaining` is declared and **never set** — and its test cannot fail

```
$ curl -s localhost:8000/metrics | grep '^rate_limit_remaining'
(nothing — only the HELP and TYPE lines are present)

$ grep -rn "rate_limit_remaining" src/ | grep -v observability/metrics.py
src/governance/ratelimit.py:214:        ``rate_limit_remaining`` gauge.   <- a docstring, not a call
```

A labelled `Gauge` with no `.labels(...)` call renders its `# HELP` and `# TYPE` lines and nothing else. The
gauge is a declaration that no code feeds.

**And the integration test that guards it is near-tautological:**

```python
# tests/integration/test_scaffold.py:163
assert "rate_limit_remaining" in body, "the per-connector gauge is missing"
```

`body` contains the `# HELP rate_limit_remaining …` line whether or not a single sample was ever recorded, so
this assertion passes against exactly the broken state it was written to catch. The phase file's acceptance
gate — *"a `/metrics` scrape shows … the `rate_limit_remaining` gauge"* — is currently met on a comment line.
Phase 4 feeds the gauge **and tightens the assertion to require a labelled sample with a value**.

---

## F6 — the instrumentator's label names are **not** the ones the phase file states

The phase file asks for `http_requests_total{route,code}`. Measured:

```
# HELP http_requests_total Total number of requests by method, status and handler.
# TYPE http_requests_total counter
```

`prometheus-fastapi-instrumentator`'s defaults label it `{handler, method, status}`. Two other named metrics
do not exist at all: `query_duration_seconds` and `connector_fetch_duration_seconds{connector}`.

**Resolution:** take the library's label names as-is and correct the phase file. Renaming them would mean
replacing the library's default collectors with hand-written ones purely to match a spelling — cost with no
behavioural gain, and it would put the golden signals on a code path nobody else exercises. The two missing
histograms are ours to add, and *those* get the names the phase file states (ADR-040).

---

## What this pass changed

| # | Finding | Effect |
|---|---|---|
| F1 | k6 v2.3.0: `handleSummary` writes through a bind mount; `data.metrics.*.values` has the percentiles | Script takes **zero** remote imports; `docs/k6-summary.txt` is written by the run |
| F2 | JSONL spans reassemble into a tree; batch-flush truncates the tail | Renderer must pick a **complete** trace, not the latest |
| F3 | No per-connector or join spans | `federation.py` gets three spans (ADR-037) |
| F4 | Mocks have no simulated latency, against HLD line 39 | Latency added on the **live path only** (ADR-038) |
| F5 | `rate_limit_remaining` unfed; its test passes on a `# HELP` line | Feed the gauge; tighten the assertion |
| F6 | Instrumentator labels are `{handler,method,status}`, not `{route,code}` | Correct the phase file; add the two real missing histograms |

Four of six are gaps between a locked document and the code. None was visible from reading the phase file —
all six came from running the thing.

## Next step

→ `docs/kickoff/v5/architecture.md` (ADR-037 onward).
