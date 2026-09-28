# Handoff

> Last updated: 2026-09-28 (Session 1)

## Project

Take-home for Ema: **Universal SQL across enterprise apps** — a federated query layer that runs one cross-app
SQL query (GitHub PRs ⋈ Jira issues) end-to-end with query-time entitlement (RLS/CLS compiled into the AST),
per-tenant rate limiting, staleness-controlled caching, and honest partial-result degradation.

This session produced **no application code**. It produced the design set the build will follow. A new agent's
job is to start building Phase 0.

## Plan Status

No plans created yet — `docs/phoenix-development-workflow/plans/` and `specs/` are empty. The design set is
complete and committed.

| Phase | Tier | Spec | Plan | Status | Commits |
|---|---|---|---|---|---|
| 0 — scaffold + contracts | MUST | `docs/design/phases/phase-0-scaffold.md` | — | Not started | — |
| 1 — connectors + governance | MUST | `docs/design/phases/phase-1-connectors.md` | — | Not started | — |
| 2 — SQL pipeline | MUST | `docs/design/phases/phase-2-sql-pipeline.md` | — | Not started | — |
| 4 — observability + load + README | MUST | `docs/design/phases/phase-4-observability.md` | — | Not started | — |
| 3 — UI console + Playwright | SHOULD | `docs/design/phases/phase-3-ui-e2e.md` | — | Not started | — |

Phase 4 is listed before Phase 3 on purpose — see Key Decisions.

## What Was Done This Session

| Commit | What |
|---|---|
| `91e6df3` | Brought the source-of-truth docs into the repo: `take_home.md` (the brief), `design-doc.md` (full, incl. §6 prototype + §8 appendix), the 5 diagrams, and the OSS prior-art cards. Rewrote relative links (`../../` → `./`) now that siblings live alongside. The 7 prototype specs are byte-identical to their originals. |
| `6c50403` | Wrote `02-DEFINITION-OF-DONE.md` (submission gate, five hard parts → one command each, MUST/SHOULD/COULD tiers, cut order). Merged a Python hook overlay into `.claude/settings.json`. Closed 4 structural spec gaps. |
| `66a6e66` | Folded in 8 deferred spec fixes; closed 4 deliverables that the phase sequence consumed but no phase built; added the scope ledger + hour ladder. |
| `e71f414` | Wrote `03-BUILD-PROCESS.md` — the per-phase command loop and what each step may decide. |

**Read these four, in this order, before doing anything:**
1. `docs/design/00-PROTOTYPE-HLD.md` — what the prototype must prove (§1) and the provenance rails (§9)
2. `docs/design/01-EXECUTION-PLAN.md` — build order + the **scope ledger** ("where am I, what's left")
3. `docs/design/02-DEFINITION-OF-DONE.md` — the submission gate and scope tiers
4. `docs/design/03-BUILD-PROCESS.md` — the exact commands, and the locked-decision list

## What Didn't Work

Nothing was abandoned — no code was written. But four things were **found wrong and fixed**, and the pattern
behind them is the main thing to carry forward:

- **Three phase specs described behaviour that could not have passed.** Phase 3's `cls_mask_visible` asserted
  `••••` while the seeded mask was `hash` (the cell value is an MD5 digest); `freshness_knob` expected a cache
  hit on the first run after a `beforeEach` that flushes Redis; and the CLS mask was specified twice, in two
  different stages. **Lesson:** when a spec asserts a rendered value, check what the data layer actually
  produces.
- **The adapter `fetch()` order spent a rate-limit token before checking the cache**, which silently falsified
  three other guarantees in the same document. Fixed to cache → token → secret.
- **Four deliverables were consumed by the phase sequence and built by no phase** — most importantly
  `src/control_plane/`, which three phases read through. It existed only as a line in the repo-layout tree.
- **The hook config was TypeScript-shaped.** Its config guard would have blocked Phase 0's `pyproject.toml`
  outright, and the documented `npm test` commit gate was never installed. My first replacement gate was also
  inert because it invoked `python`, which is not on PATH here (only `python3`).

## What's Next

- **Immediate next action: the Phase 0 AST spike.** `docs/design/phases/phase-0-scaffold.md` opens with it.
  One throwaway script, hardcoded dicts, **no** FastAPI/Postgres/Redis/Docker: `parse_one` → `qualify` → AND an
  RLS `exp.EQ` into the WHERE → rewrite one projection to `MD5(...) AS reporter_email` → flatten the top-level
  `exp.And` and group predicates by owning table → `duckdb.register()` two pyarrow tables and run the residual
  join. 60–90 minutes. **If it doesn't behave as `research/prototype-prior-art.md` Card 1 claims, Phase 2's
  design changes** — so this runs before anything else, including research.
- Then the Phase 0 loop from `03-BUILD-PROCESS.md`: lightweight `/github-research` → `/architecture-extraction`
  → `/spec` → `/plan-phase` (**hard gate: human reviews the plan**) → `/build-phase`.
- Plan file: none yet. Next: **Phase 0**.
- Last completed: the design set (4 commits above).

**Two open questions for the user — ask before building, they change Phase 0's shape:**
1. **Effort lever.** As specified this is ≈18–24h (≈15–20h for MUST-only: 0→1→2→4), against the brief's ~6–10h
   target. Dropping Phase 3 saves 3–4h (`make demo` carries the demo); dropping the whole COULD list saves
   another 2–3h, landing near 12–14h. Which lever?
2. **The submitted Google Doc is missing §6 and §8.** `synced-gdoc/.../01_tab-1.md` runs 1→2→3→4→(5)→(7) with no
   §6 *Prototype & Scenario Walkthrough* and no §8 *Appendix* — so the artifact reviewers read is missing the
   canonical query, the response envelope, the six-code error table, the policy YAML, the DDL, **and §6.4, which
   is where the access grant to `souvik-sen@` / `careers@` is stated** (submission checklist lines 50/165). Both
   sections exist in `docs/design/design-doc.md`. This is a paste, and it is the highest-value fix available.

## Key Decisions

- **Build order is 0 → 1 → 2 → 4 → 3.** Phases 3 and 4 both depend only on Phase 2, and Phase 4 holds four
  MUST-tier deliverables (k6, Prometheus metric, trace, README) while Phase 3 is SHOULD-tier. File names keep
  their original numbers.
- **Sequential, not parallel.** The phases *are* separable by directory (`src/main.py` is the only shared file),
  and P1 ∥ P2a is a clean seam if wanted — but the human plan-review gate serializes the work anyway, P2a's
  design depends on the spike outcome, and the saving is 2–3h of 18–24h. Full reasoning in
  `03-BUILD-PROCESS.md`.
- **Front-load observability.** Build the OTel span decorator and Prometheus registry in Phase 0, decorate each
  stage in Phase 2 *as it is written*, leaving Phase 4 as only k6 + artifacts + README. Free — no dependency
  change, no merge conflict.
- **Locked decisions, not to be re-opened by any step:** the stack, mock-only connectors, policy as JSONB
  predicate AST, the equijoin, RLS `assignee = :user`, CLS mask `reporter_email` with `mask: hash`, the canonical
  query verbatim, every rail in HLD §9, and the three non-negotiables in `02-DEFINITION-OF-DONE.md` §4. A step
  that thinks one is wrong **stops and says so** rather than quietly building something else.
- **Skip `/grilling`** (the brief is the requirements) and **do not run `/kickoff`** (its Phase-1 gate wants that
  grilling) or **`/project-setup`** (would overwrite the hand-tuned `CLAUDE.md`). Invoke `/github-research`,
  `/architecture-extraction`, `/spec` directly.

## Watch-outs

- **`python` is not on PATH — use `python3`** (or `.venv/bin/python` once it exists). This is what made the first
  version of the commit-test gate silently pass everything.
- **Hooks are live and will block you** (`.claude/settings.json`, all verified): files over 500 lines block the
  commit; `pytest -q tests/unit` must pass to commit (no-ops until `tests/unit/test_*.py` exists); adding
  `ignore`/`noqa`/`disable`/`strict = false` to a config is blocked, though *creating* `pyproject.toml` is fine;
  `ruff format` runs on every `.py` edit.
- **HLD §9 is the anti-drift rail.** Every value pinned there (mask kind, tiebreaker, cache TTL semantics, budget
  profiles, persona row counts, `ENTITLEMENT_DENIED` vs default-deny) is depended on by more than one phase.
  Changing one silently breaks another phase's test.
- **`FederationEngine` must stay split** into `federation.py` (DuckDB) + `assemble.py` (envelope/freshness/
  cursor). As originally specced it owned six responsibilities and would breach LAW 1 and LAW 3 — the commit hook
  blocks it at 500 lines.
- **`docs/design/design-doc.md` is a reference copy, not the submission.** Deliverables 1 and 2 are the Google
  Doc; this repo is deliverables 3 and 4. `02-DEFINITION-OF-DONE.md` §5 has the mapping, and the README must say
  it in one line or a reviewer will be confused.
- **Two personas, not one, carry the RLS demo:** alice 3 rows, bob **1** row (not 0 — a count that collapses to
  zero is indistinguishable from a broken query), carol 0 rows for the `empty` leg of the trichotomy.
- **`tenant_acme`'s GitHub budget is deliberately 5 req/60s** for the 429 demo. k6 must target `tenant_load`, or
  the load run is 30k throttled requests measuring nothing.
- **The canonical query cannot demonstrate CLS** — it projects four columns and none is `reporter_email`. That is
  why the console ships a second "CLS demo" preset. Do not "fix" this by editing the canonical query; HLD §9 pins
  it to design-doc §6.1 and changing it desyncs the two deliverables.
- **The README grows per phase**, starting in Phase 0. It is brief deliverable #4 and a submission gate; writing
  it only at the end means a Phase 4 slip loses a required deliverable.
