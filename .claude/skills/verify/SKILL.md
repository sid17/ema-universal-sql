---
name: verify
description: "Use before declaring a task, milestone, or phase done. Trigger whenever the user says 'is this done', 'verify', 'check if it works', 'are we good', or claims completion without showing evidence. Also triggered automatically by build-phase at phase boundaries."
when_to_use: "Before declaring a task, milestone, or phase done. Called by build-phase and /dev orchestrator."
domain: dev
category: evaluation
inputs: "Plan file with checkboxes + current code state"
outputs: "PASS/FAIL verification report"
handoff: "code-review"
argument-hint: "[task|milestone|phase]"
---

# Verification Before Completion

No completion claims without fresh verification evidence. Prevents premature "done" declarations.

## Context

The Iron Law of Verification: every claim of completion must be backed by fresh evidence. Run the command, read the output, check the exit code — then and only then declare done. Common rationalizations ("should work now", "I'm confident", "just cosmetic") are explicitly banned.

**Dual role:** Verify is embedded in build-phase as the final step at phase boundaries (runs automatically). It is also available standalone via `/dev verify` for on-demand verification at any time.

## Instructions

### Step 1: Determine Scope

Based on `$ARGUMENTS`:
- **task** — verify a single task's acceptance criteria
- **milestone** — verify all tasks in a milestone + cross-task integration
- **phase** — full verification of entire phase (all milestones + system-level checks)

### Step 2: Iron Law Verification (per task)

For each task being verified, apply the 5-step gate:

1. **Identify** the verification command (from the plan's "Verify:" line)
2. **Run** the command
3. **Read** the full output (not just exit code)
4. **Check** exit code = 0
5. **Then and only then** claim the task is done

### Step 3: Mechanical Checks

Run these automatically:
- [ ] All plan checkboxes for the scope are marked `[x]`
- [ ] Test suite passes (`npm test` / `pytest`)
- [ ] No files over 500 lines
- [ ] No hardcoded secrets (grep for api_key, password, secret, token patterns)
- [ ] No orphan TODO/FIXME without linked ticket
- [ ] Type checker passes (`tsc --noEmit` / `mypy`)

### Step 3a: Flow-Level Verification

Mechanical checks verify parts work. Flow verification confirms the system works. For milestone and phase scope, include at least one end-to-end flow test:

- Trace a primary user flow through the full system (e.g., "send message → refresh → messages still visible")
- If the plan has a `## Verification Gate` section, run those exact commands and check expected output
- Bugs live in transitions between features, not in individual features — test the transitions

### Step 3b: Visual / Output Verification (if applicable)

For phases that produce reviewable output (UI, server, generated data files), mechanical checks alone are insufficient. Add:
- [ ] App/server starts without errors
- [ ] Output matches the plan's verify criteria (node counts, layout, colors, interactions)
- [ ] Prompt the user to review: "Open the app and check against the verify criteria. Does it look correct?"

If the plan has specific verify criteria (e.g., "~64 visible nodes, LR flow layout"), check each one explicitly — don't just confirm the build passes.

If the user says "skip visual check" or "I've already verified this", accept their confirmation and proceed — document the skip in the verification report under a "Manual Confirmation" section.

### Step 4: Artifact Checks

- [ ] Architecture docs updated (if structural changes made)
- [ ] Plan file checkboxes match actual completion state

### Step 5: Rationalization Prevention

Rationalization phrases substitute confidence for evidence — each one has historically caused missed bugs. Required action for each:
| Excuse | Required Action |
|--------|----------------|
| "Should work now" | Run the test. Show the output. |
| "I'm confident" | Confidence is not evidence. Run the test. |
| "Just needs cleanup" | Cleanup IS the work. Do it, then verify. |
| "Only cosmetic" | Visual changes need visual verification. |
| "Same pattern as before" | Each instance must be independently verified. |
| "Tests would pass" | Then run them and prove it. |

### Step 6: Report

Output a verification report:

```
## Verification Report — {scope}
**Status:** PASS / FAIL
**Date:** {date}

### Checks
- [x] All plan tasks complete (N/N)
- [x] Tests pass (X passed, 0 failed)
- [x] No files over 500 lines
- [x] No hardcoded secrets
- [ ] FAIL: 2 orphan TODOs found in src/utils.ts

### Failures (if any)
1. {what failed, evidence, suggested fix}
```

## Gates

- [ ] Every verification claim backed by command output
- [ ] All mechanical checks pass
- [ ] Artifact checks pass

## Anti-Patterns

- Always run verification commands before declaring done — claims without evidence are the primary source of false completion.
- Reject rationalization phrases ("should work", "I'm confident") — these bypass the evidence requirement and hide unverified assumptions.
- Include artifact checks alongside code checks — outdated docs and mismatched plan states cause confusion in the next session.
- Only mark checkboxes when evidence shows the acceptance criteria is met — unchecked assumptions compound into larger failures downstream.

## Output Format

**Report:** Verification report with PASS/FAIL status, check results, and failure details (if any)
