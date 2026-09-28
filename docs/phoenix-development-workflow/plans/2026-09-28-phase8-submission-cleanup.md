# Phase 8 — Submission Cleanup

> **Goal:** make the repository readable by someone who has never seen it, and regenerate every
> artifact against final code. The claim being proved:
>
> > **A reviewer can clone, run one command, read one page, and understand what this is —
> > without opening a design document.**
>
> **Depends on:** Phase 6 (connector realism) and Phase 7 (demo coverage matrix), both in flight in
> another session. Two tasks are hard-blocked on them and are marked so.
>
> **Assumes:** nothing in this phase changes behaviour. Every code edit is a comment or docstring.
> If a test result changes, something was edited that should not have been.
>
> **Verify:** `pytest -q tests/unit tests/integration` unchanged; `ruff check src tests scripts`
> clean; `grep -rE "ADR-[0-9]|design-doc|HLD|Phase [0-9]" src/ config/ Makefile` returns nothing.

---

## Why this phase exists

The repository is written for the person who built it. Two measurements:

| | Count |
|---|---|
| `README.md` | **631 lines, 26 sections** |
| Docstrings + comments in `src/` and `scripts/` | **3,295 of 9,758 lines — 33%** |
| References a reader cannot follow (`ADR-NNN`, `design-doc §`, `HLD §`, `brief line N`, `LAW N`, `Phase N`, `v3-research`) | **341 across 58 files** |

A reference like `(ADR-024)` or `see Phase 5b` is a pointer into a document the reviewer does not
have and would not read if they did. The reasoning those pointers carry is usually *correct and
worth keeping* — it just has to be said in the comment instead of cited from it.

The README has the same problem at a larger scale: it re-states `docs/design/design-doc.md` at
length, so the one file a reviewer is guaranteed to open is also the longest.

---

## Design

### The cut/keep rule

**Cut — replace with a self-contained sentence:**

- Document pointers: `ADR-NNN`, `design-doc §X`, `HLD §Y`, `DoD §Z`, `brief line N`, `03-BUILD-PROCESS`
- Process pointers: `Phase N`, `the phase file`, `v3-research Finding N`, `LAW N`
- Process history where it adds nothing to the code: *"measured, not assumed"*, *"this cost a whole
  load run in Phase 5b"*, *"the phase file's original sketch"*

**Keep — untouched:**

- **Invariants**: *"cache before token, never token before cache"*, *"`register()` is cursor-local"*,
  *"this runs BEFORE `qualify()`"*
- **Gotchas that prevent a bug**: *"`to_arrow_table()`, not `.arrow()`, on duckdb 1.5.x"*,
  *"`scope` is space-delimited per RFC 8693"*
- **Measured numbers that justify a design**: *"a cursor costs 0.01ms against 6.5ms for an
  instance"* — the number is the argument, and it survives without naming which run produced it.

**The test for a rewritten comment:** it must make sense to a reader who has only this repository.
If deleting the citation leaves the sentence meaningless, the sentence was citing rather than
explaining, and it should be rewritten or dropped — not left with a dangling reference.

### Decisions taken

| # | Decision | Why |
|---|---|---|
| **D1** | **References only.** Comment *volume* is not the target. | The density is high but most of it is load-bearing. A pass that also compressed the essays would be making judgment calls about which invariants matter, file by file, on the day before submission. Wrong risk to take. |
| **D2** | `tests/` is **out of scope** | Test comments explain why an assertion exists, which is exactly the context a reader of a test needs. The references there are less harmful because a reviewer reads tests selectively, not front to back. |
| **D3** | Config and `Makefile` are **in scope** | `README.md` links `config/policies.yaml` as the policy config a reviewer should read, so its comments are reviewer-facing. The `Makefile` is where they run every command. |
| **D4** | README **shrinks to ~120 lines**; cut material is not relocated | All of it already exists in `docs/design/design-doc.md`. Moving it would create a third copy to keep consistent. |
| **D5** | The **final load run replaces** the numbers in `LOAD-RESULTS.md` | One verification run, and `docs/artifacts/load/` matches the document byte for byte. A table averaging runs whose artifacts are not committed is unverifiable. |
| **D6** | Artifacts regenerate **after** Phases 6 and 7, never before | Phase 6 rewrites connector internals and Phase 7 adds a demo artifact. A trace captured now pictures code that will not ship. |
| **D7** | `make e2e` is **deleted**, not documented | It is a no-op that echoes "Playwright UI specs land in Phase 3". A target that does nothing is worse than an absent one. |

---

## Sequencing rule

**Phases 6 and 7 have landed, so nothing is blocked.** The order below is driven by data
dependencies instead.

**Code comments run before the load test, not after.** Every `src/` edit here is behaviourally
neutral, but `make load-mt` rebuilds the image at the start of each run — editing source between
scenarios would measure a different image per scenario. Settling the code first means all five runs
describe one build.

**The load test runs before the README.** The README's load paragraph and `LOAD-RESULTS.md` both
quote its numbers, so writing them earlier means writing them twice.

The screenshot (C5) is last. It pictures the trace, so it cannot precede the trace's regeneration.

---

## Milestone A — the README

- [ ] **T800 — rewrite `README.md` to ~120 lines**
  Nine sections, in this order:

  1. **What this is** — 4 lines. One cross-app SQL query over two SaaS sources, with entitlement,
     rate limits and freshness.
  2. **What's built** — bullets mapped to the prototype requirement list, one line each.
  3. **What's mocked** — two short notes: the connectors are in-memory behind a real adapter seam;
     the identity provider is a stand-in that mints unsigned persona tokens.
  4. **Run it** — `make up && make seed`, then **one `curl` with its actual response inline**.
  5. **Try more** — `make demo` and `make demo-detail`, plus a link to `config/policies.yaml` as the
     policy config behind the row counts.
  6. **The trace** — the waterfall screenshot and a two-line note on what it shows.
  7. **The load test** — three lines and links to `LOAD-TESTING.md` / `LOAD-RESULTS.md`.
  8. **Where to look** — a trimmed layout table.
  9. **Design docs and repository access.**

  **Cut** (all duplicated from `docs/design/design-doc.md`): "What it proves — the five hard parts",
  "Trade-offs and the join strategy", "Production mapping", "The connector — a real connector with
  an in-memory transport", "The one assertion that matters most", both "Onboarding a connector"
  sections, and the phase-by-phase "Current status".
  **Files:** `README.md`
  **Verify:** under 130 lines; every command in it runs as written on a fresh clone; every link
  resolves; no `ADR`/`Phase N`/`§` reference survives.

## Milestone B — self-contained code comments

Same rule for every task; only the scope differs. Each ends with the full suite green, because a
botched docstring edit breaks a module import rather than a test assertion.

- [ ] **T801 — B1: `src/gateway/`, `src/models/`, `src/sqlparse/`, `src/config.py`, `src/main.py`**
  **84 references across 13 files.** Densest: `routes.py` (14), `whitelist.py` (4, including
  `v3-research Finding 4`), `errors.py` (4).
  **Watch-out:** `deps.py` documents the L1–L4 gate layers — that table is the clearest explanation
  of the authorization model anywhere in the repo. Keep it; only drop the phase citations in it.

- [ ] **T802 — B2: `src/execution/`, `src/planner/`, `src/entitlement/`, `src/pipeline/`**
  **81 references across 11 files.** Densest: `federation.py` (11), `assemble.py` (10).
  **Watch-out:** `join.py` cites "non-negotiable #2" for the rule that the executed SQL is the whole
  entitled tree. That rule is the system's core safety property — restate it, never drop it.

- [ ] **T803 — B3: `src/governance/`, `src/observability/`, `src/control_plane/`**
  **67 references across 11 files.** Densest: `metrics.py` (10), `tracing.py` (7).
  **Watch-out:** `duckdb_pool.py`'s measured numbers (6.5ms vs 0.01ms) stay — strip only the
  attribution to a named phase.

- [ ] **T804 — B4: `scripts/`**
  **17 references across 4 files.** `seed.py` (8), `waterfall.py` (3), `trace.sh`, `demo.sh`.
  **Watch-out:** `demo.sh`'s banners are printed into a committed artifact, so editing them changes
  `docs/artifacts/demo/demo-output.txt`. Regenerate in C1 rather than hand-editing the artifact.

- [ ] **T805 — B5: `src/connectors/`**
  **Re-count before starting** — Phase 6 rewrote this directory after the survey, so the previous
  figure of 60 references across 12 files no longer describes it.

- [ ] **T806 — config, `Makefile`, `docker-compose.yml`**
  **32 references.** `docker-compose.yml` (10), `Makefile` (8), `policies.yaml` (5), `grants.yaml`
  (3), `rate_limits.yaml` (3), `connectors/*.yaml` (3).
  The no-op `e2e` target (D7) has already been removed — confirm rather than repeat.
  **Watch-out:** `policies.yaml` is linked from the README as the policy config a reviewer reads.
  Its comments explaining *why the predicate is a JSONB AST rather than a SQL string*, and *why
  there is deliberately no default-deny fixture*, are the most reviewer-valuable comments in the
  repo. Rewrite the citations; keep every argument.

## Milestone C — regenerate every artifact

- [ ] **T807 — `make demo`**
  Regenerate `docs/artifacts/demo/demo-output.txt` against final code.
  **Verify:** four scenes present; row counts still 3 / 1; no credential in the file.

- [ ] **T808 — `make demo-detail`**
  Run the coverage matrix and read the output end to end, not just its exit code.
  **Verify:** the Phase 7 verification gate — all six error codes, both `join_status` values,
  `masked: true`, `next_cursor: null`, no raw `reporter_email`, no credential.

- [ ] **T809 — `make trace`**
  `scripts/trace.sh` already warms the DuckDB pool across several requests before truncating the
  span log, so the captured query lands on a warm worker. **Nothing to build — this is a check.**
  **Verify:** `duckdb_join` renders at its warm cost (single-digit ms), **not ~58ms**. A large
  `duckdb_join` means the warm-up did not take and the artifact is misleading.

- [ ] **T810 — `make scrape`**
  Run **after** T807/T808 — the histograms are empty until queries have executed.
  **Verify:** the connector-time metric has samples; `rate_limit_remaining` has a value.

- [ ] **T811 — the load re-run, plus the knee**
  Three scenarios, the same flags `LOAD-TESTING.md` documents, **and two intermediate offered rates
  to locate the knee**:
  ```bash
  make load-mt MISS_RATE=0                           # S2
  make load-mt                                       # S3  (500 offered)
  make load-mt MISS_RATE=1.0 LOAD_MAX_REQUESTS=84    # S4
  make load-mt RATE=300                              # knee
  make load-mt RATE=400                              # knee
  ```
  **Why the knee runs exist.** Today the only clean point is 200 QPS (24.8ms p50) and the only
  saturated point is 500 offered (~400 achieved, ~4s p50). Nothing is measured between them, so
  "~400 req/s" is *saturation throughput* — the rate the system manages while drowning — and must
  not be described as a rate it serves well. 300 and 400 turn an unmeasured gap into a stated
  operating limit: **clean to N QPS, saturating beyond it**.
  Per D5, **the numbers this run produces replace the table in `LOAD-RESULTS.md`**, and the verdict
  and README summary are updated to match.
  **Watch-out:** if S3 now reproduces 500 QPS, that is a *finding*, not a licence to claim it —
  it would mean the ceiling is environmental, and the honest report is still "~400 typical, 500
  observed twice, not explained". Do not quietly upgrade the headline.
  **Verify:** `docs/artifacts/load/` matches every number in `LOAD-RESULTS.md`.

- [ ] **T812 — the screenshot and its note**
  The brief asks for one dashboard **or** trace screenshot plus a short note on what it proves.
  `docs/artifacts/trace/trace-waterfall.png` is the screenshot; the note is the README's trace
  section. Confirm the committed PNG is the one T809 regenerated.
  **Verify:** the PNG's `duckdb_join` bar matches the `.txt` waterfall beside it.

## Milestone D — final consistency

- [ ] **T813 — reconcile the numbers that appear in more than one place**
  Test counts (`README.md` says 591 unit; the real number after Phases 6–7 will differ), row counts,
  and every performance figure quoted outside `LOAD-RESULTS.md`.
  **Verify:** `pytest -q tests/unit tests/integration` and the README agree.

- [ ] **T814 — repository access**
  Replace the README's placeholder block with the real URL and grant read to `souvik-sen@ema.co`
  and `careers@ema.co`.
  **Verify:** a logged-out fetch of the URL 404s; both collaborators show read.

---

## Verification Gate

The phase is not done until every line below passes.

```bash
.venv/bin/python -m pytest -q tests/unit
make up && make seed && make test-integration
.venv/bin/ruff check src tests scripts

# no unfollowable reference survives in code a reviewer reads
grep -rnE "ADR-[0-9]+|design-doc|HLD |brief line|LAW [0-9]|v[0-9]-research|Phase [0-9]" \
     src/ scripts/ config/ Makefile docker-compose.yml README.md

wc -l README.md                      # under 130
grep -c "^#\{2,3\} " README.md       # 9 sections

# artifacts describe the final build
make demo && make demo-detail && make trace && make scrape
```

**Expected:** the grep returns **nothing**; tests are unchanged in count and all green; the README
is under 130 lines; every artifact regenerates without error.

---

## Watch-outs

1. **This phase must not change behaviour.** Every `src/` edit is a comment or a docstring. If the
   test count or any test result moves, revert and find out why before continuing.
2. **Docstring edits break imports, not assertions.** A stray quote takes out a whole module. Run
   the suite after each task, not at the end of the milestone.
3. **`demo.sh`'s text is committed output.** Editing its banners changes
   `docs/artifacts/demo/demo-output.txt`; regenerate rather than hand-editing.
4. **Keep the argument, drop the citation.** The failure mode is deleting a whole comment because it
   contained `(ADR-024)`. The rule is to rewrite the sentence, not remove it.
5. **Do not describe ~400 req/s as a rate the system serves well.** It is the throughput reached
   while saturated, at roughly four seconds p50. The knee runs exist to replace that ambiguity with
   a measured limit; until they land, the honest phrasing is "clean at 200, saturating by 500".
6. **Artifacts last, screenshot last of all.** Anything regenerated before the code settles pictures
   a build that will not ship.
