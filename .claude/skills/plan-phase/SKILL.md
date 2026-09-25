---
name: plan-phase
description: "Use after a spec exists and before any code execution. Trigger when the user says 'plan this', 'break this down', 'create tasks', 'start the dev workflow', or has a spec ready for implementation. First step of /dev workflow."
when_to_use: "After a spec is published (from /kickoff Phase 4 or written manually), before any code execution. First step of /dev workflow."
domain: dev
category: orchestration
inputs: "Spec from docs/phoenix-development-workflow/specs/YYYY-MM-DD-*-design.md"
outputs: "Plan file with checkboxes at docs/phoenix-development-workflow/plans/"
handoff: "build-phase"
argument-hint: "<phase-slug>"
---

# Plan Phase

Spec → milestones → tasks. Creates the execution plan that agents follow literally.

## Context

Plans are the taste gate — the human reviews the plan before any code runs. A good plan has bite-sized steps (2-5 min each), no placeholders, and exact file paths. Organized as: foundational work first (blocking), then independent user stories.

## Instructions

### Step 1: Load Context

Read these files in priority order:

**Spec input:**
1. `docs/phoenix-development-workflow/specs/` — scan for spec files (`YYYY-MM-DD-*-design.md`)
   - If multiple specs exist: use the latest by date, or the one the user specifies
   - A spec is self-contained (requirements + architecture + scope)
   - **No specs found:** STOP — tell user to run `/kickoff` or write a spec first

**Additional context:**
- Past decisions: `docs/memory/choices/` (if exists)
- Existing plans: `docs/phoenix-development-workflow/plans/` (to avoid conflicts)

### Step 2: Map File Structure

Before defining tasks, map ALL files to create or modify:
```
Files to create: src/models/user.ts, src/routes/users.ts, tests/users.test.ts
Files to modify: src/app.ts (add route), src/types/index.ts (add User type)
```
Decomposition decisions are locked at this stage.

### Step 3: Design Phases

Organize work into phases:
1. **Setup** — project scaffold, config, dependencies
2. **Foundational** (BLOCKING) — shared infrastructure (DB schema, auth, core utils). Must complete before any stories start.
3. **User Stories** (independent, prioritized P1/P2/P3) — each story is independently testable and deliverable. Mark parallelizable tasks with `[P]`.
4. **Polish** — error handling, edge cases, performance

### Step 4: Write Plan File

Save to `docs/phoenix-development-workflow/plans/YYYY-MM-DD-NN-phaseN-slug.md`. Use the same date prefix as the spec, plus a sequence number `NN` that links plans to their parent spec (e.g., spec `2026-05-20-01-workspace-ui-mvp-design.md` → plans `2026-05-20-01-phase0-server-scaffold.md`). When multiple specs share the same date, scan existing specs to determine `NN`. This ensures `ls` on the plans directory gives you execution order.

Each plan file must include:
- Phase metadata header: **Goal**, **Depends on**, **Assumes**, **Verify** criteria
- **Assumes** states what this phase expects from prior phases (e.g., "Assumes Phase 1 produces the SDK's own session_id, not a generated UUID"). If the assumption is wrong, the dependency chain is visible.
- File Map table (all files to create/modify/delete)
- Tasks with checkboxes, file paths, and verification commands
- **Decision** field on any task involving a choice: `**Decision:** [what we chose] because [why]`. Tasks without decisions become guesses during build, and guesses become bugs.
- **Step 0: Inspect Real Data** as the first task when a phase touches external data (APIs, SDKs, file formats). Run `head -5`, `print()`, or equivalent to verify actual data shape matches the spec. No coding until you've seen the real data.
- **Verification Gate** section at the end with exact commands and expected output. The phase isn't done until the gate passes — regardless of whether individual task checkboxes are checked. Gates should test end-to-end flows, not just individual features.

See `references/plan-template.md` for the full template format with examples.

### Step 4b: Reference-Based Tasks

If the spec has a "Reference Alignment" section (meaning a reference implementation was identified during research), apply these rules to each task:

- **Cite the source:** Each task that copies from the reference should state: "Copy from reference's `path/to/file.py`, adapt [what] because [why]"
- **Copy first, adapt second:** Structure tasks as copy → verify → adapt, not rewrite-from-scratch
- **Step 0 for data phases:** When a phase touches external data (APIs, SDKs, file formats), the first task should be "Inspect real data" — run `head -5` or `print()` to verify the actual format matches the spec's data contracts

### Step 5: Self-Review Checklist

Before presenting to human:
- [ ] Every requirement in spec has ≥1 task
- [ ] No placeholder text ("TBD", "TODO", "similar to Task N", "implement later")
- [ ] Every task has Files + verification command
- [ ] Foundational tasks block stories (not parallelized)
- [ ] [P] markers only on truly independent tasks (different files, no shared state)
- [ ] Every phase has at least one test step

### Step 6: Present for Review

Show the plan to the human. **Do not proceed to build until the human approves.** This is a hard gate.

## Gates

- [ ] Every PRD requirement traced to ≥1 task
- [ ] Zero placeholder text in plan
- [ ] Human has reviewed and approved

## Anti-Patterns

- Every task needs a verification command — tasks without verify steps can't be objectively confirmed as complete, leading to false "done" claims.
  - BAD: `- [ ] Implement user registration` (no verify step)
  - GOOD: `- [ ] Implement user registration` + `Verify: npm test -- --grep registration` with expected output
- Replace all placeholder text ("TBD", "TODO", "similar to Task N", "implement later") with concrete content — placeholders defer thinking to build time when context is lost.
- Get human review before any coding starts — the plan is the taste gate, and building on an unreviewed plan wastes effort on the wrong approach.
- Keep each step atomic with its own checkbox and verify command — bundled steps hide partial completion and make it impossible to know what's actually done.
  - BAD: `- [ ] Set up database, create schema, seed data, and write migration tests`
  - GOOD: Four separate checkboxes, each with its own verify command
- Use the hybrid phase model (foundational blocking, then independent stories) rather than organizing purely by tech layer — tech layers create artificial dependencies between unrelated features.

## Output Format

**File:** `docs/phoenix-development-workflow/plans/YYYY-MM-DD-NN-phaseN-slug.md` (plan with checkboxes, date + sequence number matches source spec)
