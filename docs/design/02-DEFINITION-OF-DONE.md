# Definition of Done — the submission gate

> **This document:** the single answer to *"are we submittable?"* The phase specs each have their own green
> gate; nothing else says when the **prototype as a whole** is finished. This does.
>
> Read after [`00-PROTOTYPE-HLD.md`](./00-PROTOTYPE-HLD.md) (what we're proving) and
> [`01-EXECUTION-PLAN.md`](./01-EXECUTION-PLAN.md) (build order). Line references are to
> [`take_home.md`](./take_home.md).

---

## 1. Submission gate

All six must be true. This is the brief's own checklist (lines 47–52, 163–167), nothing invented.

| # | Gate | Brief | Verified by | Owner phase |
|---|---|---|---|---|
| 1 | Design doc + diagrams delivered | 49, 164 | the Google Doc (**not** this repo — see §5) | — |
| 2 | Repo link, read access granted to `souvik-sen@ema.co` + `careers@ema.co` | 50, 165 | manual, on submission day | P4 |
| 3 | Quickstart runs locally, containerized, cold → serving in < 60s | 51, 166 | `make up` on a fresh clone, timed | P0 / P4 |
| 4 | 1–2 tests, runnable, green | 51, 166 | `make test` | P0–P2 |
| 5 | One metrics/trace screenshot + a short note on what it proves | 52, 167 | `docs/trace-waterfall.png` + `docs/k6-summary.txt` + the README paragraph | P4 |
| 6 | README: quickstart + rationale for key trade-offs | 16 | read it end-to-end once, out loud | P4 |

**The reviewer test:** fresh clone → `make up && make seed` → `make demo` → read the trace. (With Phase 3 built,
substitute "open the console → Run" for `make demo`.) If any step needs a human to explain it, gate 6 is not met.

## 2. The five hard parts, each provable by one command

HLD §1 claims five things. A claim with no command behind it does not count. This table is the demo script.

| # | Hard part | The one command / click | Passing looks like |
|---|---|---|---|
| 1 | Query-time RLS/CLS entitlement | `make demo` — or console: run as **alice**, then **bob** | Row count goes **3 → 1** (shrinks, stays non-zero); `reporter_email` never appears raw; the Jira adapter received `assignee=<persona>`, so forbidden rows were never fetched |
| 2 | Per-tenant fairness over rate limits | `make demo` drain step — or spam **Run** | `429 RATE_LIMIT_EXHAUSTED` + `Retry-After` + a `suggested_action` naming the async path. Never a hang |
| 3 | Credential isolation | `pytest tests/unit/test_secrets.py` | Each tenant's `secret_ref` decrypts to its own token under its own Fernet key; no cross-load |
| 4 | Entitlement-aware caching / freshness | `pytest tests/integration/test_staleness_knob.py` — or console: `max_staleness_ms` 0 → 60000 | `sources[].served` flips `live` → `cache`, no token spent on the hit; `freshness_ms` reports the **stalest** contributor |
| 5 | Connector reliability + honest degradation | `pytest tests/integration/test_timeout_partial.py` | `partial: true`, `join_status: incomplete`, GitHub rows present, un-joined rows never passed off as joined |

**Plus the correctness point that is graded but easy to lose:** `pytest tests/integration/test_trichotomy.py`
keeps **empty ≠ partial ≠ error** distinct (HLD §5).

## 3. Scope tiers

Tag every deliverable before building, so an overrun is a decision already made rather than a panic. The
hour boxes in the execution plan are ordering, not deadlines — this is what protects the submission when
they slip.

### MUST — the brief asks for these explicitly. Not submittable without them.

| Deliverable | Brief |
|---|---|
| `POST /v1/query` → `rows`, `columns`, `freshness_ms`, `rate_limit_status`, `trace_id` | 152–153 |
| Minimal policy config: **1 RLS rule + 1 column mask** | 154 |
| **2 connectors** (mocked is explicitly fine) | 156 |
| User token → scopes/roles → RLS/CLS | 157, 61 |
| Token bucket **with burst**; friendly error + async-reroute guidance | 158, 110 |
| `max_staleness` knob showing **cache hit vs live fetch** | 159, 61 |
| SQL subset: projection, filters, **pagination**, joins | 19 |
| Timeouts + **partial results** | 24 |
| Error vocabulary | 67 |
| Join strategy documented (federated vs materialized) | 69 |
| k6 ~500–1k QPS for 60s, local acceptable | 160 |
| 1 Prometheus metric + 1 trace showing **connector time** | 161 |
| Containerized quickstart + 1–2 tests | 51, 166 |
| Metrics/trace screenshot + note | 52, 167 |
| Repo access granted | 50, 165 |

### SHOULD — not demanded, but this is what makes it read as senior work.

- **UI console + Playwright specs.** The brief says *keep UI minimal* (62), so the console is a demo
  vehicle, not a requirement. It is still the fastest way for a reviewer to *see* four of the five hard
  parts, and the screenshot artifact comes free. **Committed as of 2026-09-28** — this is being built, not
  held in reserve. **Ordering consequence:** Phase 3 builds it and is SHOULD-tier, while Phase 4 holds four
  MUST-tier items and depends only on Phase 2 — so **Phase 4 runs before Phase 3 regardless**, not only under
  time pressure (see the scope ledger in `01-EXECUTION-PLAN.md`).
- **Cross-tenant cache isolation test.** Cheap, and it is the sharpest security point available
  (Security is 15%).
- **Result-level cursor pagination** over the joined rows, on top of the MUST-level `LIMIT`.
- **Audit-log row per query** — the access trail behind "compliance signals" (45).
- **`make demo`** — a curl script printing the envelope for alice / bob / forced-timeout. Insurance: it
  is a submittable demo of all five hard parts *before* any UI exists.

### COULD — build only if the MUSTs and SHOULDs are green with time left.

These are all real engineering, and all of them are things the design doc already covers in prose. Prose
is an acceptable answer for every item here.

- ETag / `304` conditional revalidation (the design's §4.3 middle path)
- Connector-level pagination simulation (Link-header / `startAt`) beyond a simple cursor
- Three *nested* token buckets — the brief asks for *a* token bucket with burst (158); one composite key satisfies it
- The airbyte-derived error machinery: action enum, `failure_type`, circuit breaker, `Retry-After` backoff
- `test_crypto_shred` and the second tenant beyond the isolation test
- Single-flight coalescing guard
- Residency seeding / `deployment_mode` column exercise

### Cut order

> **Scope decided 2026-09-28 (after Phase 0 shipped): all five phases — 0 → 1 → 2 → 4 → 3.** The SHOULD-tier
> console and Playwright specs are committed, not conditional. The COULD list below stays **opportunistic**:
> those are individual deliverables rather than a phase, and prose remains an acceptable answer for each.
>
> Committing to full scope does **not** retire this cut order — it makes it more important, because the
> estimate it protects (≈18–24h against the brief's ~6–10h framing) is the one most likely to slip. The tiers
> are what turn an overrun into a decision already made rather than a panic.

If time runs short, cut from the bottom of COULD upward, and **never** cut into SHOULD before COULD is
empty. Every cut item must get its README paragraph mapping it to where the full design covers it
(HLD §7) — that is what keeps a scoped-down prototype reading as *deliberate* rather than *unfinished*.

## 4. Non-negotiables regardless of time

Three things are correctness, not scope. If any is false, the prototype argues against its own design doc
and is worse than a smaller prototype that holds the line.

1. **Entitlement is compiled into the plan and pushed down — never post-filtered.** The moment we fetch
   rows we aren't entitled to and drop them in Python, the central claim of the design doc is false.
2. **Pushdown is an optimization; the engine re-applies every predicate authoritatively**, and fetches
   `projection ∪ every WHERE/ORDER BY column` so re-filtering can't drop a valid row.
3. **empty ≠ partial ≠ error.** Three distinct envelope shapes, three tests.

## 5. What this repo *is*

Stating it so a reviewer doesn't mistake a reference copy for a deliverable:

| Brief deliverable | Where it lives |
|---|---|
| 1. High-level design + diagrams | the **Google Doc**. `docs/design/design-doc.md` is a reference copy for traceability |
| 2. Six-month execution plan | the **Google Doc** (§7). Not this repo's `01-EXECUTION-PLAN.md`, which is the *prototype build order* |
| 3. Prototype | **this repo** — `src/`, `ui/`, `tests/`, `load/` |
| 4. README: quickstart + trade-off rationale | **this repo** — `README.md` |

The README must say this in one line, or a reviewer will read `docs/design/` as the submission and wonder
why the design doc is in the prototype repo.

## 6. Requirement coverage triage

Two brief requirements were neither claimed nor declared non-goals. Resolved here so nothing is silently
dropped:

- **Admin connector onboarding + versioning (29–30).** *Claimed, cheaply.* Connectors are data, not code:
  `connectors.capabilities` is JSONB and `connectors.version` is a column, both loaded at seed from
  `config/connectors/*.yaml`. Onboarding a connector = one YAML file + one adapter class implementing
  `fetch()`. An admin *console* for it is a documented non-goal. The same YAML loader covers the
  "minimal policy config" (154) that design-doc §6.2/§8.2 says ships as YAML.
- **Cost controls (10, 38, bonus 177).** *Design-doc scope, not prototype scope.* Currently scattered
  across Risk 6, §5.2 and the cost table with no dedicated section. Worth one short subsection in the
  submitted doc — cost-per-query, cache-hit ratio as the primary lever, projection pushdown, the 128 MB
  spill threshold, per-tenant spend caps, admission control. No prototype code implied.
