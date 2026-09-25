---
name: dev
description: "Use when starting or resuming a development task. Activate whenever the user mentions building, coding, implementing, planning work, deploying, or wants to check development status — even if they don't say '/dev' explicitly."
when_to_use: "Starting or resuming a development workflow. The main entry point."
domain: dev
category: orchestration
inputs: "PRD from kickoff (or existing plan/state)"
outputs: "Deployed production app"
handoff: "none"
argument-hint: "[plan|build|verify|review|deploy|handoff|status]"
---

# /dev — Software Development Orchestrator

State-checking phase router. Reads current state, determines the right phase, routes accordingly.

**Escape hatch:** If the user says "skip" or "just build", proceed to the requested phase without ceremony.

## Context

The /dev orchestrator is the single entry point for the full development workflow. It reads HANDOFF.md + plan checkboxes to determine where you are, then routes to the appropriate phase. Handles fresh starts, resumptions, and re-entries. See `references/project-types.md` for phase skipping by project type.

## Instructions

### On Invocation: Determine Current State

1. Check for `docs/phoenix-development-workflow/handoff.md`:
   - **Exists:** Read it. Display the Plan Status table. Auto-route to the next phase.
   - **Does not exist + plan files have checked items:** Warn: "No HANDOFF.md found but plan has progress — state may be lost from a previous session. Scanning plan files to determine current phase."
   - **Does not exist + no checked items (or no plans):** Fresh project, proceed normally.
2. Check for plan files in `docs/phoenix-development-workflow/plans/` — scan checkboxes for progress.
3. Check for specs or PRD (input for plan-phase):
   - **Primary:** `docs/phoenix-development-workflow/specs/*.md` — self-contained design specs (from `/kickoff` Phase 4 or written manually)
   - **Neither exists:** No PRD — stop and tell user to run `/kickoff` or write a spec first

### Phase Router

Based on state, route to the appropriate phase:

| State | Route To | Action |
|-------|----------|--------|
| No spec or PRD in `docs/phoenix-development-workflow/specs/` | STOP | Tell user to run `/kickoff` or write a spec first |
| Spec/PRD exists, no plan | **plan-phase** | Decompose spec into plan |
| Plan exists, not reviewed | STOP | Ask human to review plan (hard gate) |
| Plan reviewed, tasks unchecked | **build-phase** | Execute next unchecked task |
| Build stuck (3 attempts) | **debug** | Systematic debugging |
| All tasks checked | **code-review** | Two-stage review (verify is embedded in build-phase) |
| Review approved | **deploy** (user-invoked) | Prompt user to run `/deploy` |

### Hard Gates (Cannot Skip)

1. **Plan must be human-reviewed** before build starts
2. **Tests must pass** before deploy

### Soft Gates (Recommended, Can Skip)

1. **Code review** before deploy — recommended, user can override
2. **Design review** for UI projects — recommended for frontend changes

### Phase Overrides

User can force a specific phase via argument:
- `/dev plan` — go to plan-phase regardless of state
- `/dev build` — go to build-phase
- `/dev verify` — run standalone verification
- `/dev review` — run code review
- `/dev deploy` — run deploy (user-invoked)
- `/dev handoff` — run /handoff to capture session state
- `/dev status` — show current state without changing anything

### Session Lifecycle

1. **Session start:** Read `docs/phoenix-development-workflow/handoff.md` → display status → route to current phase
2. **During session:** Execute tasks via build-phase (includes embedded verify + REVIEW.md)
3. **Phase completion:** Prompt: "Run /handoff to checkpoint, or continue to next phase?"
4. **Session end:** Prompt: "Run /handoff to save state for next session?"

## Hook Rules

- **Stop hooks must be silent on success.** A Stop hook that produces output triggers another response — infinite loop. If nothing to report, exit 0 with no output.
- **Validation gates belong on PreToolUse**, matching the action they guard.

## Commit Rule

**Always ask before committing.** Do not auto-commit after completing a phase or task. Present what was done and ask: "Ready to commit?"

## Gates

- [ ] Current state correctly determined before routing
- [ ] Hard gates enforced (plan reviewed, tests pass)

## Anti-Patterns

- Create a plan before coding — coding without a plan leads to ad-hoc implementation that misses requirements and produces inconsistent architecture.
- Get human review on plans before building — the plan is the taste gate, and building on an unreviewed plan wastes effort on the wrong approach.
- Verify tests pass before deploying — deploying untested code risks production incidents that are harder to diagnose than pre-deploy test failures.
- Only deploy when the user explicitly invokes it — auto-deployment removes human judgment from the highest-risk step in the workflow.
- Always ask the user before committing — auto-commits remove the user's chance to review changes before they become part of the project history.

## Output Format

**Routed to:** The appropriate phase skill (plan-phase, build-phase, debug, code-review, deploy, or handoff)
