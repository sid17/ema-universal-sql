---
name: phoenix-development-feedback
description: "Capture process reflection after a development workflow run."
when_to_use: "After completing a /dev run or milestone. User must explicitly invoke."
trigger: /phoenix-development-feedback
---

# Phoenix Development Feedback

Capture a structured process reflection after a `/dev` run or milestone. Generates a `meta.md` artifact that can be processed via `/skillsmanager-improve` in phoenix-skills.

---

## How to Run

1. User invokes `/phoenix-development-feedback`
2. Determine the context from recent plans in `docs/phoenix-development-workflow/plans/` and any HANDOFF.md
3. Read all relevant artifacts to ground the reflection
4. Generate the feedback artifact

---

## Output

Write to: `meta/phoenix-development/<run-id>/meta.md`

Use the plan slug or date as run-id (e.g., `2026-05-20-mvp-scaffold`).

---

## Feedback Questions by Phase

### Plan Phase (plan-phase)
- Did the plan decompose the spec into appropriately sized tasks?
- Were dependencies between tasks identified correctly?
- Did the plan template produce a useful structure, or was it overhead?
- Were any tasks discovered during build that should have been in the plan?

### Build Phase (build-phase)
- Did embedded verify catch real issues or create false friction?
- Was REVIEW.md useful for communicating what changed?
- Did the build phase handle task dependencies in the right order?
- Were there any points where the agent got stuck and debug should have been invoked earlier?

### Debug (debug)
- Was the 4-phase escalation structure appropriate for the bugs encountered?
- Did debug correctly identify root causes, or did it chase symptoms?
- Were there bugs that required a different debugging approach entirely?

### Verify (verify)
- Did the Iron Law (no completion without evidence) catch real gaps?
- Were verification steps proportional to the risk of the change?
- Did verify ever block progress unnecessarily?

### Code Review (code-review)
- Did the two-stage review (scope drift + quality) catch meaningful issues?
- Were scope drift warnings accurate, or false alarms?
- Was review timing right (before deploy), or should it have happened earlier?

### Gen Tests (gen-tests)
- Were generated tests meaningful or just coverage padding?
- Did tests match the project's testing conventions?
- Were edge cases covered, or only happy paths?

### Security Review (security-review)
- Were security findings relevant to this project's threat model?
- Did the OWASP checklist surface anything non-obvious?
- Were any findings false positives that wasted time?

### Deploy (deploy)
- Did config-driven deployment work smoothly?
- Were health checks appropriate and correctly configured?
- Any deployment steps that should be automated but weren't?

### Handoff (handoff)
- Did the handoff capture enough context for session resumption?
- Was any critical context lost between sessions?
- Did the orchestrator resume correctly from the handoff state?

### Cross-Phase
- Was the overall pacing appropriate?
- Did the orchestrator route to the correct phase at each transition?
- Were hooks (format, type-check, tests) helpful or disruptive?
- Did the code-quality rule catch real issues or create noise?

---

## Output Template

```markdown
# Meta — Process Reflection & Development Methodology

## The Problem We Were Solving
One paragraph: what feature/milestone was this dev run addressing?

## The Process We Followed

### Plan Phase (PRD → Execution Plan)
**What we did:** numbered list of concrete actions taken
**Output:** list of artifacts produced (with paths)
**Process insight:** what worked, what implicit pattern made it effective

### Build Phase (Task Execution)
**What we did:** ...
**Output:** ...
**Process insight:** ...

### Debug (if invoked)
**What we did:** ...
**Output:** ...
**Process insight:** ...

### Verify (Embedded + On-Demand)
**What we did:** ...
**Output:** ...
**Process insight:** ...

### Code Review (Scope Drift + Quality)
**What we did:** ...
**Output:** ...
**Process insight:** ...

### Gen Tests (Test Generation)
**What we did:** ...
**Output:** ...
**Process insight:** ...

### Security Review (if invoked)
**What we did:** ...
**Output:** ...
**Process insight:** ...

### Deploy (if invoked)
**What we did:** ...
**Output:** ...
**Process insight:** ...

### Handoff (Session State Capture)
**What we did:** ...
**Output:** ...
**Process insight:** ...

## The Core Methodology (Abstracted)
Numbered list extracting the reusable process from this run.

## What Made This Process Work
Numbered insights — named, explained, grounded in this run.

## How to Improve This Process
Numbered suggestions — specific, actionable, justified.

## Applicability Beyond This Project
| Phase | Generic Form | Project-Specific Form |
|-------|-------------|----------------------|
| Plan | Spec decomposition into tasks | ... |
| Build | Task execution with embedded checks | ... |
| Debug | Systematic 4-phase escalation | ... |
| Verify | Evidence-based completion | ... |
| Review | Two-stage quality gate | ... |
| Tests | Convention-aware test generation | ... |
| Deploy | Config-driven with health checks | ... |
| Handoff | Session state serialization | ... |

## Session History
| Session | Date | What Happened | Key Output |
|---------|------|---------------|------------|
| ... | ... | ... | ... |
```
