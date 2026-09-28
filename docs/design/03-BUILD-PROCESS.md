# Build Process — how we get from these specs to running code

> **This document:** the agreed sequence of commands, per phase, and what each one is allowed to decide.
> It exists so the process is repeatable across sessions and so no step quietly re-opens a locked decision.
>
> Order of reading: [`00-PROTOTYPE-HLD.md`](./00-PROTOTYPE-HLD.md) → [`01-EXECUTION-PLAN.md`](./01-EXECUTION-PLAN.md)
> (scope ledger) → [`02-DEFINITION-OF-DONE.md`](./02-DEFINITION-OF-DONE.md) (submission gate) → this file.

---

## The loop, per phase

For each phase N in 0..4, run these five steps. Steps 1–3 are kickoff-workflow skills; steps 4–5 are
dev-workflow. Nothing in steps 1–3 writes code.

```
┌─ per phase N ─────────────────────────────────────────────────────────────────┐
│                                                                                │
│  1. /github-research   (lightweight)   → docs/kickoff/v<N+1>/research-repos.md │
│           ↓            scoped to this phase's unknowns only                    │
│  2. /architecture-extraction           → docs/kickoff/v<N+1>/architecture.md   │
│           ↓            FORMALIZE §A/§B — do not re-decide                      │
│  3. /spec                              → docs/phoenix-development-workflow/    │
│           ↓                              specs/YYYY-MM-DD-phaseN-<slug>.md    │
│  4. /dev  → /plan-phase                → plans/YYYY-MM-DD-phaseN-<slug>.md    │
│           ↓            ★ HARD GATE: human reviews the plan before any code     │
│  5. /dev  → /build-phase → /verify → /code-review → /dev handoff               │
│                                                                                │
└────────────────────────────────────────────────────────────────────────────────┘
```

Then the phase's own green gate (from `phases/phase-N-*.md`) must pass before phase N+1 starts.

### Invoke the skills directly — do not run `/kickoff`

The `/kickoff` orchestrator gates Phase 2 behind Phase 1, and its Phase-1 gate is *"all 10 grilling categories
Resolved or Deferred."* We are deliberately skipping `/grilling` (the brief **is** the requirements — see
`CLAUDE.md`), so the orchestrator would stall or want to run it. Invoke `/github-research`,
`/architecture-extraction` and `/spec` directly instead.

Two more operational notes:

- **Do not run `/project-setup`.** `dev-workflow.md` suggests it for first-time projects, but `CLAUDE.md`
  already exists and is hand-tuned for this assignment. Running it risks overwriting it.
- **`/spec` expects a requirements input.** There is no `requirements.md` because we skipped grilling. Point it
  at [`take_home.md`](./take_home.md) + the phase file + `02-DEFINITION-OF-DONE.md` §3 tiers instead.

### Kickoff version ↔ phase mapping

Kickoff versions are per-project, so we borrow them as phase slots:

| Kickoff version | Phase | Slug |
|---|---|---|
| `v1` | Phase 0 | `scaffold` |
| `v2` | Phase 1 | `connectors` |
| `v3` | Phase 2 | `sql-pipeline` |
| `v4` | Phase 3 | `ui-e2e` |
| `v5` | Phase 4 | `observability` |

---

## What each step is allowed to decide

This is the part that matters. Each step has a narrow mandate; exceeding it is how a build drifts away from the
design doc we're submitting.

### 1. `/github-research` — lightweight, and only for genuine unknowns

Most of this is already done. `01-EXECUTION-PLAN.md` §D records four repos deep-read with the concrete decisions
each one changed, and the cards live in [`research/prototype-prior-art.md`](./research/prototype-prior-art.md):

| Card | Repo | Already settled |
|---|---|---|
| 1 | `sqlglot` | `qualify()` first; manual per-source split (**not** the `pushdown_*` passes); `(db, name)` table mapping; node-whitelist validator; `exp.EQ` + `.where()` for RLS, `proj.replace(alias_(func(...)))` for CLS |
| 2 | `airbyte-python-cdk` | `RequestOption` injection primitive; pagination = strategy ⊕ placement; error mapping = action enum + `failure_type`; `Retry-After`-driven backoff |
| 3 | `universql` | `duckdb.register(name, pyarrow.Table)` → SQL over registered names; `:memory:` per request |
| 4 | `fastapi-permissions` | Adopt the `configure_permissions` DI factory shape only — its object-level post-fetch checks are exactly what our invariant bans |

**So per phase, research is a 10–20 minute confirmation pass, not a survey.** Cap it at that. The only phase
with a real open question is **Phase 4** (OTel FastAPI instrumentation shape, k6 script structure). If a phase has
no open question, write one line saying so and move to step 2 — an empty `research-repos.md` is a valid outcome.

**Hard rule:** research may *inform* how we implement a locked decision. It may not reopen the decision.

### 2. `/architecture-extraction` — formalize, don't re-decide

The architecture is already chosen. `01-EXECUTION-PLAN.md` **§A** is the finalized stack with sources and **§B**
is the deviation log with reasons. That is an ADR set in everything but format.

So this step's mandate is: **convert §A + §B into `architecture.md` as ADRs already marked Accepted**, each citing
its §D research card or its §B rationale. It is a formatting and traceability pass.

**Locked — not open for re-decision by any step:**

- Python 3.11 · FastAPI · sqlglot · DuckDB · Redis · Postgres · Fernet · Playwright · k6 · OTel + Prometheus
- Mock connectors only; `BaseConnectorAdapter.fetch()` is the single live-adapter seam
- Policy as **JSONB predicate AST**, never a SQL string
- Equijoin `pr.issue_key = issue.key`; RLS `jira.issues.assignee = :user`; CLS mask `reporter_email` (`hash`)
- The canonical query, verbatim, as in HLD §4 = design-doc §6.1
- Every provenance rail in **HLD §9**
- The three non-negotiables in **`02-DEFINITION-OF-DONE.md` §4**

If a step believes a locked decision is wrong, it **stops and says so** rather than quietly building something
else. Changing one means changing the HLD, the phase specs, *and* the submitted design doc together.

### 3. `/spec` — one spec per phase, into the dev-workflow inbox

Output goes to `docs/phoenix-development-workflow/specs/`, which is exactly where `/plan-phase` looks. That
handoff is what makes the two workflows one pipeline.

The phase files in [`phases/`](./phases) are already spec-grade — deliverables, typed contracts, acceptance tests,
a "Done when" gate. So `/spec` is largely a conversion. Its job is to add what a phase file doesn't carry:

- the phase's **MUST/SHOULD/COULD** tiering from `02-DEFINITION-OF-DONE.md` §3, task by task
- explicit **file paths** against the HLD §8 repo layout
- the **test list** as named files, so `/plan-phase` can make them checkboxes
- which **README section** this phase fills (see Phase 0's README-growth table)

**Sanity check before accepting a generated spec:** if it contradicts the phase file, the phase file wins —
or the contradiction is a real bug in the phase file and gets fixed there first, not forked into the spec.

### 4. `/plan-phase` — the hard gate

Produces a checkboxed plan in `plans/`. **A human reads it before any code runs.** This is the last cheap place
to catch a misunderstanding; after this, mistakes cost build time.

Check the plan for: tasks ordered so each is independently verifiable; tests in the *same* task as the code
(LAW 6); no task that would produce a file over ~400 lines (LAW 1 — the commit hook blocks at 500).

### 5. `/build-phase` → `/verify` → `/code-review` → handoff

- `/build-phase` executes tasks one at a time, calls `/debug` on repeated failure, calls `/gen-tests`, and emits
  `REVIEW.md` for confirmation before committing.
- `/verify` is embedded as the final step — the Iron Law: run it, read the output, *then* claim it works.
- `/code-review` does scope-drift + quality. Run `/security-review` too at the end of **Phase 2**, since that's
  where entitlement lands and Security is 15% of the rubric.
- `/dev handoff` before stopping; `/dev` to resume. Session state lives in
  `docs/phoenix-development-workflow/handoff.md`.

**Active hooks** (verified installed, `.claude/settings.json`): destructive-command block · 500-line file-size
gate · secret block · config-weakening guard (creating `pyproject.toml` is fine; adding `ignore`/`noqa`/`disable`
is blocked) · `pytest -q tests/unit` commit gate · `ruff format` on every `.py` edit.

---

## Sequential or parallel?

**Verdict: go sequentially — 0 → 1 → 2 → 4 → 3 — but front-load observability.** The reasoning matters more than
the answer, because the phases *are* separable and it is worth knowing why we're not exploiting that.

### The phases are cleanly separable by directory

Extracted from the specs, file ownership barely overlaps:

| Phase | Owns |
|---|---|
| 0 | `docker-compose.yml` · `Makefile` · `pyproject.toml` · `001_init.sql` · `src/models/` · `src/gateway/` · `src/control_plane/` · `src/main.py` |
| 1 | `src/connectors/` · `src/governance/{ratelimit,cache,secrets}.py` · `config/**` · `002_seed.sql` |
| 2 | `src/sqlparse/` · `src/entitlement/` · `src/planner/` · `src/execution/` · `src/pipeline/` · `src/governance/audit.py` · `scripts/demo.sh` |
| 3 | `ui/` · `tests/e2e/` |
| 4 | `src/observability/` · `load/` |

**`src/main.py` is the only genuinely shared file** — P0 creates it, P2 rewires `/v1/query` to the real runner,
P3 mounts the static console, P4 mounts real `/metrics`. Everything else is disjoint.

### So parallelism is possible — here is the only clean seam

**P1 ∥ P2a**, where *P2a* is the pure-AST half of Phase 2 (`SQLParser`, `EntitlementEngine`, `QueryPlanner`).
Those three operate on a sqlglot AST plus a capability **dict**; they never call an adapter, so every unit test in
Phase 2's list except the integration ones can be written with no Phase 1 code present. Then *P2b*
(`FederationEngine`, `assemble`, `runner`, integration tests, `make demo`) joins the two tracks.

Preconditions, all three required:
1. **Phase 0 is complete and `src/models/` is frozen** — both tracks code against that contract.
2. **`pyproject.toml` declares every dependency up front** (Phase 0's spec already lists them all), so neither
   track edits it.
3. P2a uses an **inline capability fixture** — the one the AST spike already produces — and P2b swaps it for the
   real `control_plane.get_capabilities()` read.

### Why we're not doing it anyway

- **The human plan-review gate serializes the work regardless.** Two concurrent tracks means two plans to review
  at once and two `REVIEW.md` files; the bottleneck is attention, not CPU.
- **P2a's design depends on the spike's outcome.** If `qualify()` + predicate-split-by-owning-table doesn't behave
  as §D Card 1 claims, P2a gets reshaped — parallel work started early is work rewritten.
- **The saving is small against the risk.** Maybe 2–3h off an 18–24h build, in exchange for a merge step on the
  one shared file every other phase also touches.

Revisit only if a second person (or a worktree-isolated agent) takes Phase 1 end-to-end while you take P2a.

### Front-load observability instead — this is the real win

Don't retrofit instrumentation in Phase 4. Phase 0 already stubs `GET /metrics`; extend that slightly:

- **Phase 0** also builds the OTel span decorator and the Prometheus registry, so the seams exist from day one.
- **Phase 2** decorates each stage *as it writes it* (`parse_ms`, `entitlement_ms`, `plan_ms`,
  `connector_*_ms`, `duckdb_join_ms`) — one line per stage while the code is fresh, instead of re-reading five
  modules later to find the boundaries.
- **Phase 4** then shrinks to what only it can do: run k6, capture the trace waterfall and `/metrics` scrape,
  finish the README. Roughly 1–1.5h instead of 2–3h, and the instrumentation is better because it was written by
  whoever wrote the stage.

This is free: it changes no dependency and creates no merge conflict.

## Phase tracker

Tick as each phase's green gate passes. Tier and gate detail live in `01-EXECUTION-PLAN.md`'s scope ledger.

| | Phase | Tier | 1. research | 2. arch | 3. spec | 4. plan | 5. build | Gate passed |
|---|---|---|---|---|---|---|---|---|
| ☑ | **0** scaffold + contracts | MUST | ☑ | ☑ | ☑ | ☑ | ☑ | ☑ |
| ☐ | **1** connectors + governance | MUST | ☑ | ☑ | ☑ | ☐ | ☐ | ☐ |
| ☐ | **2** SQL pipeline | MUST | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| ☐ | **4** observability + load + README | MUST | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| ☐ | **3** UI console + Playwright | SHOULD | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| ☐ | **Submission gate** (`02-DEFINITION-OF-DONE.md` §1) | — | | | | | | ☐ |

**Phase 4 is listed before Phase 3 deliberately.** Both depend only on Phase 2, and Phase 4 holds four MUST-tier
deliverables (k6, the Prometheus metric, the trace, the README) while Phase 3 is SHOULD-tier. Build in tracker
order, not numeric order. Phase file names keep their original numbers.

## Where everything lands

| Artifact | Path | Written by |
|---|---|---|
| Research notes | `docs/kickoff/v<N>/research-repos.md` | `/github-research` |
| ADRs | `docs/kickoff/v<N>/architecture.md` | `/architecture-extraction` |
| Phase spec | `docs/phoenix-development-workflow/specs/*.md` | `/spec` |
| Phase plan | `docs/phoenix-development-workflow/plans/*.md` | `/plan-phase` |
| Session state | `docs/phoenix-development-workflow/handoff.md` | `/dev handoff` |
| Build review | `docs/phoenix-development-workflow/REVIEW.md` (temporary) | `/build-phase` |
| Code | `src/` `ui/` `tests/` `load/` `config/` `scripts/` | `/build-phase` |
| Submission artifacts | `docs/demo-output.txt` `docs/trace-waterfall.png` `docs/k6-summary.txt` `docs/console.png` | P2, P4, P3 |

## Effort reality check

The phase specs inherit the brief's ~6–10h framing. Against what is now specified, the honest estimate is
**≈18–24h**, or **≈15–20h** for the MUST-only path (0 → 1 → 2 → 4). Two levers were available if that needed
to come down: drop Phase 3 entirely (−3–4h; `make demo` already carries the demo), and drop the whole COULD
list (−2–3h), landing near 12–14h.

### DECIDED (2026-09-28, after Phase 0 shipped): build all five phases

**Neither lever is being pulled.** The scope is the full tracker — **0 → 1 → 2 → 4 → 3** — including the
SHOULD-tier UI console and Playwright specs. Budget accepted at ≈18–24h against the brief's ~6–10h framing;
the decision was taken with Phase 0's real cost already known rather than as an up-front guess.

What this does and does not change:

- **Build order is unchanged.** Still 0 → 1 → 2 → 4 → 3. Phase 4 keeps its place ahead of Phase 3 because it
  holds four MUST-tier deliverables (k6, the Prometheus metric, the trace, the README) and both depend only on
  Phase 2. Committing to Phase 3 is not a reason to build it earlier.
- **The COULD list stays opportunistic.** "All phases" means all five *phases*; the COULD items in
  `02-DEFINITION-OF-DONE.md` §3 are individual deliverables, not a phase, and prose remains an acceptable
  answer for every one of them. Build them only once every MUST and SHOULD is green.
- **The cut order still stands** and is not dead weight. It is insurance, not a plan: if the hour boxes slip,
  cut from the bottom of COULD upward and **never** into SHOULD before COULD is empty.
- **Nothing about the tiers changes.** A MUST is still a MUST. The tiers are what protect the submission when
  estimates slip, and the estimate above is the one most likely to.

Revisit only if a phase gate slips badly enough that a MUST-tier deliverable is at risk — at which point
Phase 3 is the first thing to go, because `make demo` already carries the demo.
