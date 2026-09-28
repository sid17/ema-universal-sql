# Phase 7 — The Demo Coverage Matrix

> **Goal:** ship a second, exhaustive walkthrough artifact that answers the brief's
> *Prototype Requirements (detailed)* list **line by line**, as input → output, so a reviewer can
> tick every box without reading source. The claim being proved:
>
> > **Every capability the design doc publishes is reachable, and here is the call that reaches it.**
>
> **Depends on:** Phase 2 (entitlement), Phase 3 (the gateway + test hooks), Phase 4 (metrics),
> Phase 5b (the k6 artifact). **Sequenced after Phase 6** — §7 and the error table read cleaner once
> the connector work settles, and Phase 6 edits the `Makefile` too (T616).
>
> **Assumes:** the stack is up (`make up`), seeded (`make seed`), and `TEST_MODE=1` — the forced
> failure hooks are 404 without it. `bash` + `curl` + `python3` only, like `scripts/demo.sh`: a
> reviewer runs this on a fresh clone and it adds no dependency.
>
> **Verify:** `make demo-detail` writes `docs/artifacts/demo/demo-detail.txt`; the file contains
> all six `ErrorCode` values, three distinct "no rows for you" shapes, and no plaintext credential.

---

## Why this phase exists

`scripts/demo.sh` is a **narrative**: four calls, four hard parts, ~60 seconds of a reviewer's
attention. It is deliberately short and should not grow — the moment it tries to be exhaustive it
stops telling a story.

But exhaustiveness is separately valuable, and the gap is wide. The seed fixtures were authored
with demonstrations in mind that **nothing currently runs**:

| Fixture | Comment says | Exercised today |
|---|---|---|
| `policies.yaml` `deny-jira-issues-auditor` | "`ENTITLEMENT_DENIED` is REACHABLE … a declared code no seeded state can produce is a claim, not a feature" | **no** |
| `policies.yaml` default-deny note | a `contractor` role yields `200` + `rows: []` | **no** |
| `grants.yaml` globex-has-no-jira | "the one-line demonstration" of `CONNECTOR_NOT_ENABLED` | **no** |
| `auth.py:48` empty-scopes token | "how the scope gate is tested" | unit tests only |
| `mock_data.py` persona table | `carol` = 0 rows, the `empty` leg of the trichotomy | **no** |
| `routes.py:175` `/v1/query/async` | the 501 a 429's `suggested_action` points at | **no** |

`demo.sh` itself closes with a **"Not shown here"** banner naming two hard parts it skips. This
phase is that banner, discharged.

The second reason is that three different reasons for "you don't get these rows" — *denied* (403),
*default-denied* (200, `rows: []`), and *entitled but empty* (200, `rows: []`, different cause) —
are individually unremarkable and collectively the sharpest thing in the system. They are only
legible **side by side**, which is a thing only this artifact can do.

---

## Design

### Two documents, two jobs

| | `demo-output.txt` | `demo-detail.txt` |
|---|---|---|
| Shape | narrative, 4 calls | coverage matrix, ~30 scenes |
| Reader | first 60 seconds | ticking off the brief |
| Question it answers | "does this work?" | "does it do *everything* asked?" |
| Changes when | the story changes | a requirement is added |

`demo.sh` is **not modified**. The two coexist; `make artifacts` runs both.

### The scene contract

Straight-line bash for ~30 scenes lands near LAW 1's 500-line limit and buries the coverage list
inside curl syntax. So each scene is one declarative call:

```bash
scene "2.5" "auditor role, canonical query" \
      "403 ENTITLEMENT_DENIED — deny overrides allow" \
      --user dana --role auditor --sql "$CANONICAL"
```

`scene` prints the banner, mints the token, issues the call, pretty-prints the envelope, and
records the id in a coverage tally. **The list of `scene` calls is the deliverable** — it reads as
the requirement checklist it is, and the runner stays ~60 lines.

### Determinism

`demo.sh` resets once at the top. That is not enough here: §3 drains `tenant_acme`'s GitHub bucket
(capacity 7 — `max_requests: 5` + `burst: 2`), and §4 depends on cache state it must control. So
**every section begins with `POST /v1/test/reset`**, and §3 runs last among the state-mutating
sections regardless.

### Decisions taken

| # | Decision | Why |
|---|---|---|
| **D1** | A second script, not a `--verbose` flag on `demo.sh` | Different readers, different failure modes. A flag makes the narrative version's output a subset of a file nobody reads twice, and couples two documents that change for unrelated reasons. |
| **D2** | Table-driven `scene` calls, not inline curl blocks | LAW 1, and because the call list *is* the coverage claim. A reviewer should be able to read the scene list alone and know what is covered. |
| **D3** | k6 is **referenced, not re-run** | 60s+, needs `tenant_load` seeded, and `docs/artifacts/load/k6-summary.txt` already exists. Re-running it here would triple the artifact's runtime to reproduce a file that is already committed. |
| **D4** | `config/policies.yaml` is **printed inline**, before the scenes it governs | The brief asks for "a minimal policy config expressing one RLS rule and one column mask" — that is a request to see the *config*, not only its effect. Printing it next to the 3→1→0 row counts is the whole argument in one screen. |
| **D5** | The `sql`-only reading of "sql **or** plan JSON" is stated in the artifact **header** | `QueryRequest` accepts `sql` only (`src/models/request.py:9`). "Or" makes that defensible; silence about it does not. A reviewer who notices it unaided scores it worse than one who reads us naming it. |
| **D6** | Per-section reset, not per-scene | Per-scene reset would destroy §4 entirely — the cache-hit scene *requires* the previous scene's entry to survive. |
| **D7** | Scenes assert nothing; they print | This is an artifact, not a test suite. `tests/integration/` owns assertions. A script that both demonstrates and asserts fails the demo when a test fixture drifts, which is the wrong coupling — the reviewer sees a stack trace instead of an answer. |

---

## File map

| File | Action | Task |
|---|---|---|
| `scripts/demo_detail.sh` | create — the runner + scene helper | T700 |
| `scripts/demo_detail.sh` | modify — §1 interfaces | T702 |
| `scripts/demo_detail.sh` | modify — §2 entitlements | T703 |
| `scripts/demo_detail.sh` | modify — §3 rate limits | T704 |
| `scripts/demo_detail.sh` | modify — §4 freshness | T705 |
| `scripts/demo_detail.sh` | modify — §5 error vocabulary | T706 |
| `scripts/demo_detail.sh` | modify — §6 SQL subset | T707 |
| `scripts/demo_detail.sh` | modify — §7 observability + §8 load pointer | T708 |
| `docs/artifacts/demo/demo-detail.txt` | create (generated) | T709 |
| `Makefile` | modify — `demo-detail` target, added to `artifacts` | T709 |
| `docs/artifacts/README.md` | modify — one row | T710 |
| `README.md` | modify — one line in the artifacts list | T710 |

**No file in `src/` is touched.** This phase is `scripts/` + `docs/` only.

---

## Sequencing rule

**T700 and T701 land before any scene.** Every later task is "add `scene` calls to a section", so
the helper and the section skeleton must exist and be proven on one scene first. A section written
against a half-built helper gets rewritten when the helper settles.

The section order in the *file* is the brief's order. The section order in *execution* is
constrained only by D6 and the bucket drain (§3 last among mutating sections).

---

## Milestone A — the runner

- [x] **T700 — `scene` helper, banner, token minting, coverage tally**
  Port `token()`, `query()` and `banner()` from `scripts/demo.sh` rather than rewriting them —
  LAW 2. Extend `token()` to take `role`, `tenant` and `scopes` (all four are already `MockTokenRequest`
  fields; `demo.sh` hardcodes `role=support`, `tenant=tenant_acme`).
  **Decision:** `scenes` are numbered `§.N` matching the brief's bullets, and the tally prints at
  the end as `covered: 31 scenes across 8 sections` — so a truncated run is visible as truncated.
  **Files:** `scripts/demo_detail.sh`
  **Verify:** one smoke scene (1.1) renders a banner + envelope; `bash -n scripts/demo_detail.sh`.

- [x] **T701 — the header block**
  Artifact provenance (generated-by, `$BASE`, seed assumption, `TEST_MODE`), the section index, and
  **the D5 note** on `sql` vs plan JSON.
  **Files:** `scripts/demo_detail.sh`
  **Verify:** header names every section that follows, and no section exists that it omits.

## Milestone B — interfaces and entitlements

- [x] **T702 — §1, the envelope contract**
  `1.1` single-source GitHub query, **no join** — the simplest full envelope, `join_status: "n/a"`.
  `1.2` the canonical two-source join — `join_status: "complete"`.
  `1.3` pagination: `LIMIT 2` → page 1 → `next_cursor` → page 2 → last page with `next_cursor: null`.
  `1.4` `cat config/policies.yaml` (D4).
  **Decision:** 1.1 runs **first in the artifact**. `join_status: "n/a"` vs `"complete"` is only
  meaningful to a reader who has seen both, and the single-source case is also the cheapest
  introduction to the envelope's eight fields.
  **Files:** `scripts/demo_detail.sh`
  **Verify:** 1.1's envelope carries `rows`, `columns`, `freshness_ms`, `rate_limit_status`,
  `trace_id`; 1.3's last page has `next_cursor: null`.

- [x] **T703 — §2, token → scopes/roles → RLS/CLS**
  Eight scenes, in gate order (`deps.py:98` L2 → L3 → L4):

  | id | Input | Expected |
  |---|---|---|
  | 2.1 | `alice`, `support` | 3 rows |
  | 2.2 | `bob`, `support`, byte-identical SQL | 1 row |
  | 2.3 | `carol`, `support` | 0 rows — entitled, nothing assigned |
  | 2.4 | `alice` + `reporter_email` | MD5 digest, `masked: true` |
  | 2.5 | `role=auditor` | **403 `ENTITLEMENT_DENIED`** (deny overrides allow) |
  | 2.6 | `role=contractor` | **200, `rows: []`** (default-deny: no matching allow) |
  | 2.7 | `scopes=""` | **403**, missing `query:execute` |
  | 2.8 | `tenant=tenant_globex`, jira query | **`CONNECTOR_NOT_ENABLED`**, caught at the parser |

  **Decision:** 2.3 / 2.5 / 2.6 are adjacent and banner-labelled as *the trichotomy*. Separated,
  each looks like an ordinary result; together they are the section's whole argument.
  **Watch-out:** 2.2 must print the SQL and show it is **byte-identical** to 2.1's. If the artifact
  re-renders the string differently the proof evaporates — use the same shell variable.
  **Files:** `scripts/demo_detail.sh`
  **Verify:** row counts are 3 / 1 / 0, matching the persona contract at `src/connectors/mock_data.py:8-20`;
  2.4's output contains no `@` character in `reporter_email`.

## Milestone C — limits, freshness, errors

- [x] **T704 — §3, rate limits (runs last among mutating sections)**
  `3.1` `rate_limit_status` across successive live calls, ticking down from capacity 7.
  `3.2` the call that exhausts it → **429**, `Retry-After`, `suggested_action`.
  `3.3` `POST /v1/query/async` → **501** with its documented message.
  **Decision:** 3.3 actually issues the call rather than quoting the route. The 429's
  `suggested_action` points somewhere; an artifact that shows the pointer **resolves** — and to a
  501, not a 404 (`routes.py:175`) — proves the reroute is a scoped decision rather than a dead link.
  **Files:** `scripts/demo_detail.sh`
  **Verify:** 3.2 is reached in ≤ 8 calls (`config/rate_limits.yaml:19-23`) and carries a
  `Retry-After` header.

- [x] **T705 — §4, freshness**
  `4.1` `max_staleness_ms=0` → `served: live`, `stats.connector_ms` populated.
  `4.2` same query, `=60000` → `served: cache`, **`rate_limit_status` unchanged from 4.1**.
  `4.3` a `max_staleness_ms` smaller than the entry's age → **200 plus a `STALE_DATA` warning**.
  **Decision:** 4.2 prints 4.1's budget beside it. "A cache hit spends no token" is an *equality*
  between two responses; showing only the second asks the reader to remember a number.
  **Watch-out:** 4.3 needs `max_staleness_ms > 0` — the `STALE_DATA` branch at
  `src/execution/assemble.py:256-260` is guarded on it, so `=0` produces a live fetch and **no warning**.
  **Files:** `scripts/demo_detail.sh`
  **Verify:** the three scenes run without an intervening reset, and 4.2's `rate_limit_status`
  is byte-equal to 4.1's.

- [x] **T706 — §5, the six error codes as a table**
  One scene per `ErrorCode` (`src/models/errors.py:26-34`): four armed via `/v1/test/fail-next`
  (`timeout`, `throttled`, `auth`, `not_enabled` — `FailureMode` at `src/connectors/errors.py:61`),
  `ENTITLEMENT_DENIED` by re-pointing at 2.5, `STALE_DATA` by re-pointing at 4.3. Section closes
  with a code → HTTP status → producing-call table.
  **Decision:** `SOURCE_TIMEOUT` shows the **partial** envelope in full — `partial: true`,
  `join_status: "incomplete"`, `next_cursor: null` — because the "no cursor from a partial page"
  rail (`src/models/envelope.py:65-72`) is a validator that never otherwise surfaces in an artifact.
  **Files:** `scripts/demo_detail.sh`
  **Verify:** `grep -c` finds all six code strings in the output.

## Milestone D — subset and observability

- [x] **T707 — §6, the SQL subset boundary**
  Five rejections, each printing the caller-actionable message from `src/sqlparse/whitelist.py`:
  `SELECT *`, `GROUP BY`, a subquery, `LIKE`, `IN`.
  **Decision:** included because the messages are *designed* — "SELECT * is not supported — name the
  columns you need" explains the projection-pushdown reason. Five cheap calls showing the boundary
  was chosen rather than stumbled into.
  **Files:** `scripts/demo_detail.sh`
  **Verify:** each returns 400 and a message naming the construct.

- [x] **T708 — §7 observability, §8 load pointer**
  §7: `curl /metrics` filtered to the connector-time metric, plus a `trace_id` lifted from §1.2 with
  a pointer to `docs/artifacts/trace/`. §8: the k6 headline numbers quoted from
  `docs/artifacts/load/k6-summary.txt` with a pointer to the file and the command that regenerates it (D3).
  **Files:** `scripts/demo_detail.sh`
  **Verify:** the metric grep is non-empty; every referenced artifact path exists.

## Milestone E — wire it up

- [x] **T709 — `make demo-detail` and the committed artifact**
  Target modelled on `demo:` (`Makefile:170`), depending on `test-mode`; added to `artifacts:`
  (`Makefile:163`). Tees to `docs/artifacts/demo/demo-detail.txt`.
  **Watch-out:** Phase 6 T616 also edits the `Makefile` (adds `connectors:`). Expect a trivial
  conflict; both are single-target additions in the same style.
  **Files:** `Makefile`, `docs/artifacts/demo/demo-detail.txt`
  **Verify:** `make demo-detail` on a fresh `make up && make seed` writes a non-empty artifact.

- [x] **T710 — document it**
  One row in `docs/artifacts/README.md` distinguishing the two demo artifacts by **job**, not by
  size; one line in the root `README.md` artifact list.
  **Files:** `docs/artifacts/README.md`, `README.md`
  **Verify:** the commands run as written on a fresh clone.

---

## Verification Gate

The phase is not done until every line below passes, regardless of checkbox state.

```bash
make up && make seed
make demo-detail                                     # writes the artifact
make demo                                            # still byte-identical but for trace_id/timestamps
D=docs/artifacts/demo/demo-detail.txt
for c in RATE_LIMIT_EXHAUSTED STALE_DATA ENTITLEMENT_DENIED \
         SOURCE_TIMEOUT CONNECTOR_NOT_ENABLED CONNECTOR_AUTH_ERROR; do
  grep -q "$c" "$D" || echo "MISSING: $c"
done
grep -q '"masked": true' "$D"                        # CLS visible
grep -q '"join_status": "n/a"' "$D"                  # the single-source case
grep -q '"join_status": "complete"' "$D"             # the join case
grep -q '"next_cursor": null' "$D"                   # last page
grep -qE '@[a-z]+\.(com|io)' "$D" && echo "LEAK: raw email in artifact"
grep -qE 'ghp_[A-Za-z0-9]{8,}' "$D" && echo "LEAK: credential in artifact"
bash -n scripts/demo_detail.sh
wc -l scripts/demo_detail.sh                         # under 400 (LAW 1)
```

**Expected:** all six codes present; three distinct no-rows shapes visible in §2; no raw
`reporter_email` and no credential anywhere; the scene tally at the foot matches the header's index.

---

## Watch-outs

1. **`demo.sh` is not modified.** If a scene here needs a helper change, port the helper — do not
   refactor both scripts onto a shared library for two callers (LAW 5).
2. **§3 drains the bucket.** Anything added after it either resets first or reads a throttled
   `tenant_acme`. This is the single most likely source of a confusing re-run.
3. **§4 must NOT reset between its own scenes** (D6) — the cache-hit scene depends on the entry the
   live scene wrote.
4. **`TEST_MODE` is required.** Without it `/v1/test/fail-next` and `/v1/test/reset` are **404**, and
   `set -euo pipefail` with `curl -fsS` will abort the run at the first reset — loudly, which is
   correct, but the header should say so.
5. **The artifact is committed output.** Anything nondeterministic beyond `trace_id` and timestamps —
   an unsorted result, a wall-clock `freshness_ms` in a banner — makes every regeneration a diff.
6. **Scenes print, they do not assert (D7).** The temptation to add `|| exit 1` will grow; resist it.
   A drifting fixture should change the artifact, not fail the demo.
