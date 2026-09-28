# Phase 6b — Test Stability and the Multiprocess Gauge

> **Goal:** a suite that passes from any starting state, and a `rate_limit_remaining`
> gauge that means what it says under eight workers.
>
> **Depends on:** Phase 6 (commits `006ed4c`..`45fe6c2`).
>
> **Verify:** three consecutive `artifacts -> integration` cycles, all green; `pytest -q tests/unit`
> green; `ruff check` clean.

---

## The failure

```
FAILED tests/integration/test_metrics.py::test_the_gauge_matches_what_the_envelope_told_the_caller
1 failed, 112 passed
```

Observed twice, both times on the run **immediately after** `make demo && make connectors &&
make trace && make scrape`. Four runs from a quiet stack passed. It is not random — it is
**state-dependent**, and the state is the Prometheus multiprocess sample directory, which nothing
clears between runs.

## The mechanism

`rate_limit_remaining` is a `Gauge` declared `multiprocess_mode="livemin"`. Under eight workers:

| | what it is |
|---|---|
| the envelope's `rate_limit_status.github.remaining` | the reading of **the one worker that served this query**, taken just after it spent a token |
| `/metrics`' `rate_limit_remaining{...}` | `min()` across **every live worker's last reading** for those labels |

The test asserts these are **equal**. They are equal only when the serving worker happens to hold
the minimum. After the artifact sequence, several workers have written that label set at different
points in different drains, so an older, lower reading from another worker wins and the equality
fails.

This is the same class of bug as the forced-failure hook fixed in `9048f86`: **per-worker state read
through an endpoint that aggregates across workers.** The hook version was worse — it broke a
MUST-tier artifact. This one only breaks a test, but it breaks it by asserting something the design
cannot deliver, which is the more insidious shape: the assertion looks like a guarantee.

## The decision

**`livemin` is the wrong mode, and the test was right.**

The gauge does not measure a per-worker quantity that wants aggregating. It measures **one shared
number in Redis**, sampled by whichever worker happened to serve a query. For that shape the honest
aggregation is the **most recent observation**, not the smallest.

`livemin` also fails in the direction that costs an operator most: it reports a stale low value long
after the bucket refilled, so an alert on "budget exhausted" fires for a tenant who is fine. The
existing docstring defends `livemin` as "the honest answer"; that reasoning holds for a quantity each
worker owns a *share* of, and this is not one.

`prometheus-client` 0.26 supports `livemostrecent` (verified:
`Gauge._MULTIPROC_MODES` contains it). The `live*` prefix is still wanted — a worker that exited must
not keep voting.

**Consequence:** the equality the test asserts becomes true by construction, because the query under
test *is* the most recent write. The test is restored rather than weakened.

---

## Tasks

- [x] **T800 — reproduce it deterministically**
  A test that pins the mechanism rather than the symptom: two workers write different values for one
  label set, then assert what `/metrics` renders. Must fail under `livemin` and pass under
  `livemostrecent`.
  **Files:** `tests/unit/test_metrics_multiprocess.py` (create)
  **Verify:** the new test fails before T801 and passes after — checked in that order, not assumed.

- [x] **T801 — switch the gauge to `livemostrecent`**
  Rewrite the docstring: state what the gauge measures (one shared number, several observers), why
  `min` was wrong, and which way it failed.
  **Files:** `src/observability/metrics.py`
  **Verify:** `pytest -q tests/unit`

- [x] **T802 — restore the integration assertion, and say what it rests on**
  The equality stays. Add the sentence that makes it survive the next reader: it holds *because* the
  mode is `livemostrecent` and this query is the most recent write.
  **Files:** `tests/integration/test_metrics.py`
  **Verify:** `make test-integration`

- [x] **T803 — delete `reset_multiprocess_dir`, or wire it**
  It has **no caller**. `scripts/entrypoint.sh` already wipes `$PROMETHEUS_MULTIPROC_DIR/*.db` before
  forking, which is the only safe moment to do it — deleting those files while workers hold live
  mmaps drops readings silently. Dead code that looks like a safety net is worse than no safety net
  (LAW 5).
  **Decision:** delete, and move its reasoning into the entrypoint where the work actually happens.
  **Files:** `src/observability/metrics.py`, `scripts/entrypoint.sh`, `tests/unit/test_metrics.py`
  **Verify:** `grep -rn reset_multiprocess_dir src tests` finds nothing; `make up` still starts clean.

- [x] **T804 — look for the same shape elsewhere**
  Any other per-worker state exposed through an aggregating endpoint, or any other test asserting
  equality across workers. Report what is found; fix only what is wrong.
  **Verify:** written findings, not a clean-bill assertion.

- [x] **T805 — prove stability, do not claim it**
  Three consecutive `demo -> connectors -> trace -> scrape -> pytest tests/integration` cycles from
  one stack, captured to a file.
  **Verify:** three green runs, output kept.

---

## Verification Gate

```bash
.venv/bin/python -m pytest -q tests/unit
make up && make seed && make test-integration
for i in 1 2 3; do make artifacts && make test-integration; done   # the real gate
.venv/bin/ruff check src tests scripts
```

**Expected:** unit and integration green; three artifact->suite cycles green; no file over 400 lines
beyond the pre-existing `tests/unit/test_entitlement.py`.

## What actually happened

Two corrections to the plan, both worth keeping.

**1. The first version of T800 declared the fix broken.** It wrote readings
through the raw `MultiProcessValue` instead of `Gauge.set`, and under
`livemostrecent` every assertion failed with *no samples at all*. The cause is
in the library: `mostrecent` merges on a **timestamp**, and only
`prometheus_client.metrics.Gauge.set` records one (it branches on
`_is_most_recent`). A test that reimplements the write rather than calling the
production path would have sent this phase in the opposite direction — or worse,
passed while the real gauge rendered nothing. The test now goes through `Gauge`.

**2. A third instance of the same shape, found by T804 and NOT fixed.**
`POST /v1/test/reset` calls `repository.invalidate()`, which clears the
control-plane TTL cache on **only the worker that served the reset**; the other
seven keep theirs for up to `CONTROL_PLANE_TTL_MS`. `flushdb` in the same route
is global, so the endpoint looks uniform and is not.

It has not bitten anything — the reads it caches change only on a re-seed, and
the tests that re-seed read Postgres directly rather than through the app. But a
test that re-seeded and then queried through `POST /v1/query` would see stale
data from seven workers out of eight. The docstring now says so instead of
claiming the reset is global. Fixing it properly needs a shared invalidation
epoch, which is more than this phase should take on.

**The pattern, three times now:** per-worker state reached through an interface
that looks global. The forced-failure hook (broke a MUST-tier artifact), this
gauge (broke a test by asserting something the design could not deliver), and
the reset route (harmless today). Worth checking for deliberately whenever
`WORKERS > 1`.

## Measured

| Check | Result |
|---|---|
| `pytest tests/unit` | **689 passed** (was 683) |
| `pytest tests/integration` | **113 passed** |
| three `artifacts -> integration` cycles | **113, 113, 113** — the exact sequence that failed twice before |
| `test_metrics_multiprocess.py` under `livemin` | fails 2/6, checked in that order |
| `ruff check src tests scripts` | clean |

## Watch-out

`prometheus_client` writes one `.db` file per (metric type, pid) and **never removes them**. They
survive `/v1/test/reset`, which flushes Redis only. So metric state is the one piece of the stack
that is *not* reset between tests — any future test asserting an absolute gauge value will be
order-dependent for the same reason this one was. Assert deltas, or assert through the envelope.
