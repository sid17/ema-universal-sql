---
name: build-phase
description: "Use after plan-phase is approved and the human has reviewed the plan. Trigger when the user says 'start building', 'implement the plan', 'execute', 'build this', or has an approved plan ready for implementation. Core execution engine of /dev workflow."
when_to_use: "Plan exists and is human-approved. Core execution engine of /dev workflow."
domain: dev
category: orchestration
inputs: "Approved plan file (docs/phoenix-development-workflow/plans/*.md)"
outputs: "Implemented code + tests, updated plan checkboxes, REVIEW.md for user review"
handoff: "verify (on-demand via /dev verify)"
argument-hint: "<phase-slug>"
---

# Build Phase

**HARD GATE:** Plan must be human-approved before any code is written. If the plan has not been explicitly approved, STOP and ask.

Execute plan tasks one at a time. Quality hooks fire automatically on every edit.

## Context

The build phase picks up tasks from the plan, implements them with pre-implementation checks, and marks them done. The agent executes continuously without asking "should I continue?" — it only stops on BLOCKED status or plan completion. Verification is embedded as the final step — no separate routing to /verify needed.

## Instructions

### Step 1: Load Plan

Read the current plan file in `docs/phoenix-development-workflow/plans/`.
- Find the next unchecked `- [ ]` task
- If resuming (plan has some `[x]` marks), re-read recent code to understand current state
- If all tasks checked, skip to Step 5 (Verify + Review)

### Step 2: Pre-Implementation Check (per task)

Before writing any code for a task:
1. **Grep for existing implementations** — search codebase for functions/modules that already do what this task needs. Import existing code, don't reinvent.
   - BAD: Writing a new `formatDate()` when `utils/dates.ts` already exports one.
   - GOOD: `grep -r "formatDate" src/` → found existing, imported it.
2. **Inspect real data** — if this task touches an external data source (API, SDK, file format, database), inspect the actual data before coding. Run `head -5`, `print()`, or equivalent. Compare against what the spec/plan says. If they differ, flag the discrepancy before proceeding — don't code against assumptions that don't match reality.
3. **Read CLAUDE.md** — check project conventions (naming, patterns, file structure)
4. **Read affected files** — understand the code you're about to modify

### Step 3: Implement

1. Write code following the plan's steps
2. Write tests alongside implementation (not after)
3. PostToolUse hooks fire automatically (format, type-check)
4. If a hook fails, fix the issue before proceeding

### Step 4: Report Task Status + Continue

After each task, report one of 4 statuses:

| Status | Meaning | Action |
|--------|---------|--------|
| **DONE** | Task complete, verification passes | Mark `[x]`, continue to next task |
| **DONE_WITH_CONCERNS** | Complete but has potential issues | Mark `[x]`, note concern, continue |
| **NEEDS_CONTEXT** | Missing information to proceed | Pause, ask human for clarification |
| **BLOCKED** | Cannot complete after 3 attempts | Stop, escalate (see `references/escalation-protocol.md`) |

Mark plan checkbox `- [ ]` → `- [x]` after each completed task.

### Step 5: Verify + Review (after all tasks complete)

**Embedded verification:** Read the plan file's `**Verify:**` header. Run each verification command. Apply the Iron Law: run command → read output → check exit code → then claim done. No rationalization ("should work", "I'm confident").

**Flow test:** Before emitting REVIEW.md, run at least one end-to-end flow test that exercises the full user path through this phase's features — not just individual task verification. The acceptance test is "does the flow work?" not "do the parts work in isolation?" If the plan has a `## Verification Gate` section, run those checks.

**Emit REVIEW.md** at `docs/phoenix-development-workflow/REVIEW.md`:

```
# Review — Phase N: [Name]

## How to Run
[exact command to run/view the output]

## What to Expect
[specific, quantifiable: node counts, colors, behaviors]

## Not Yet Working
[features deferred to later phases]

## Known Issues
[edge cases, rough edges, or "None expected"]
```

**User review loop:**
1. Tell the user REVIEW.md is ready and what to run
2. User reviews → gives feedback ("looks good" or reports issues)
3. Fix any issues → re-run verification
4. User confirms phase is complete
5. Delete `docs/phoenix-development-workflow/REVIEW.md`
6. Ask: "Ready to commit?"

If user says "skip review" for non-visual phases, proceed directly to commit prompt.

## Completion Checklist

- [ ] Pre-implementation grep completed for each task
- [ ] All completed tasks have passing verification
- [ ] Plan header verify criteria checked (Iron Law)

## Anti-Patterns

- Always run pre-implementation grep before writing new code — importing existing utilities prevents duplication and keeps the codebase consistent.
- Execute continuously between tasks rather than asking "should I continue?" — pausing after every task breaks flow and adds friction without adding value.
- Stop after 3 failed fix attempts and get human approval — a 4th attempt at the same approach wastes time; the human may see the problem differently.
- Write tests alongside implementation, not after — tests written after the fact test what the code does, not what it should do, missing the bugs that matter.
- Always ask the user before committing — auto-commits remove the user's chance to review changes before they become part of the project history.

## Output Format

**Updated:** Plan file with checked boxes
**Created:** `docs/phoenix-development-workflow/REVIEW.md` (temporary, deleted after user confirms)
**Artifact:** Implemented code + tests matching plan tasks
