---
name: grilling
description: "Use at the start of any new project or feature to elicit requirements before building. Trigger whenever the user says 'new project', 'I have an idea', 'let's build', 'start from scratch', 'what should we build', or describes a problem they want to solve — even if they jump straight to implementation. Requirements come first."
when_to_use: "When starting a new project, exploring an idea, or capturing requirements before any implementation begins"
domain: core
category: investigation
inputs: "User's idea, topic, or direction to explore"
outputs: "docs/kickoff/requirements.md — structured requirements with coverage table"
handoff: "github-research | deep-research"
---

# Grilling — Structured Requirements Elicitation

## Context

Captures and sharpens requirements through taxonomy-guided questioning. Not "ask some questions" — a formal elicitation with coverage tracking and termination rules. Combines spec-kit's ambiguity taxonomy with superpowers' one-at-a-time questioning pattern and research-backed multiple-choice format.

Grilling must complete before implementation begins — building on unexamined requirements leads to rework when assumptions turn out to be wrong. All categories should be Resolved or Deferred before proceeding.

## Instructions

### Step 1: Capture Initial Context

Read any existing project context (`.claude/context.md`, `CLAUDE.md`, prior Q&A files).

**Pre-work detection:** If the user provides a design doc, spec draft, or detailed description, scan it against the 10 categories below before asking questions. Identify which categories are already covered (Resolved), which are partially covered (Partial), and which have no coverage (Outstanding). Only ask about gaps — shift from discovery mode to validation mode. Tell the user: "Your design doc covers categories [X, Y, Z]. I'll focus on [A, B, C]."

Then ask the user for (if not already provided):
- **Problem statement** — what problem are they solving?
- **JTBD framing** — "When [situation], I want to [motivation], so I can [expected outcome]"
- **Solution vision** — what's their unique angle or differentiator?

### Step 2: Taxonomy-Guided Questioning

Ask questions ONE AT A TIME across these 10 categories, prioritized by `Impact × Uncertainty` (highest first):

1. **Functional Scope & Behavior** — what does it do?
2. **Domain & Data Model** — what are the core entities and relationships?
3. **Interaction & UX Flow** — how do users interact with it?
4. **Non-Functional Quality** — performance, scale, reliability targets?
5. **Integration & External Dependencies** — what does it connect to?
6. **Edge Cases & Failure Handling** — what happens when things go wrong?
7. **Constraints & Tradeoffs** — budget, timeline, team, tech limitations?
8. **Security & Auth** — who can access what? Data sensitivity?
9. **Deployment & Ops** — where does it run? How is it monitored?
10. **Completion Signals** — what does "done" look like?

**Question format:** For each question, provide:
- 2-4 multiple-choice options
- A **recommended** option with 1-sentence reasoning
- An "other" escape hatch for custom answers

After EACH answer, update the coverage table and tag the requirement with MoSCoW priority (Must/Should/Could/Won't).

### Step 3: Track Coverage

Maintain a running coverage table. Display it after every 3-5 questions:

| Category | Status | Key Requirements |
|----------|--------|-----------------|
| Functional Scope | Resolved / Partial / Outstanding | ... |
| ... | ... | ... |

### Step 4: Termination

Grilling ends when ANY of these is true:
1. All 10 categories are **Resolved** or explicitly **Deferred** (with rationale)
2. User signals "done" / "proceed" / "stop"
3. After 15 questions: prompt user to review coverage and decide whether to continue

### Step 5: Pre-Mortem

Before finalizing, ask: "What would make this project fail? What would change your mind about this approach?" Capture risks.

### Step 6: Self-Review & Output

Run 5-point self-review on the requirements:
1. **Completeness** — any TODOs/TBDs remaining?
2. **Consistency** — contradictions between categories?
3. **Clarity** — ambiguous "maybe"/"possibly"/"might" language?
4. **Scope** — requirements for one project, not a platform?
5. **YAGNI** — requirements the user didn't ask for?

Write `docs/kickoff/requirements.md`.

## Gates

- [ ] All 10 categories are Resolved or Deferred (no Outstanding)
- [ ] `docs/kickoff/requirements.md` exists with coverage table showing all green
- [ ] Self-review passed (no unresolved flags)

## Anti-Patterns

- Run grilling even for "simple" projects — simple projects generate the most wasted work from unexamined assumptions because everyone assumes they understand the scope.
- Always provide multiple-choice options with a recommended choice rather than open-ended questions — MC options reduce cognitive load and produce more precise requirements.
- Ask one question at a time — multiple simultaneous questions lead to shallow answers and missed nuances.
- Only include requirements the user expressed or confirmed — fabricated requirements expand scope beyond what the user actually needs.
- This skill produces requirements only, not implementation — jumping to architecture or code from here skips the research phase that grounds decisions in evidence.

## Output Format

**File:** `docs/kickoff/requirements.md`

```markdown
# Requirements — [Project Name]

## Problem Statement
[JTBD: When... I want to... so I can...]

## Solution Vision
[User's unique angle / differentiator]

## Requirements by Category

### 1. Functional Scope & Behavior
- [REQ-F1] [requirement] (Must)
- [REQ-F2] [requirement] (Should)

### 2. Domain & Data Model
...

(all 10 categories)

## Coverage Table
| Category | Status | Notes |
|----------|--------|-------|

## Deferred Items
| Item | Category | Rationale |

## Pre-Mortem Risks
- [Risk 1]
- [Risk 2]

## Next Step
→ Phase 2: Research (run /kickoff to continue)
```
