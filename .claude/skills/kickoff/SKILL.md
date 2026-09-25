---
name: kickoff
description: "Use when starting any new technical project from scratch, or adding a major feature to an existing project. Activate whenever the user mentions project kickoff, starting a new project, requirements gathering, or needs to go from idea to spec — even if they just say 'let's build something new'."
when_to_use: "When starting any new technical project from scratch — before any coding begins"
domain: core
category: orchestration
trigger: /kickoff
steps:
  - id: grill
    skill: grilling
    gate: "docs/kickoff/v<N>/requirements.md exists with all categories Resolved or Deferred"
  - id: github-research
    skill: github-research
    gate: "docs/kickoff/v<N>/research-repos.md exists with comparison table"
    parallel: true
  - id: deep-research
    skill: deep-research
    gate: "docs/kickoff/v<N>/research-landscape.md exists with findings"
    parallel: true
  - id: architecture
    skill: architecture-extraction
    gate: "docs/kickoff/v<N>/architecture.md exists with at least 1 accepted ADR"
  - id: spec
    skill: spec
    gate: "docs/phoenix-development-workflow/specs/YYYY-MM-DD-*-design.md exists"
handoff: dev
---

# /kickoff — Project Kickoff Orchestrator

## Context

Chains four phases of project kickoff with state tracking and resumability. A single command that manages the full lifecycle from raw idea to a published design spec. All state lives on disk — stop anywhere, come back, pick up from where you left off.

Kickoff sessions are versioned (`v1`, `v2`, ...). Each version is a self-contained brainstorming workspace. The final output of each version is a published spec in `docs/phoenix-development-workflow/specs/`.

**When to use:** Starting any new technical project from scratch, or adding a major feature to an existing project.
**When NOT to use:** Bug fixes, small feature adds, non-technical work.

## On Invocation

### 1. Determine Version

Scan `docs/kickoff/` for existing version directories:
- **None exist:** This is v1. Create `docs/kickoff/v1/`.
- **v1/ exists and is complete** (spec published): Ask user — resume v1 or start v2?
- **v<N>/ exists and is incomplete:** Resume from last incomplete step in v<N>.
- **User passes argument** (`/kickoff v2`): Use that version directly.

### 2. Check State

Look for `docs/kickoff/v<N>/plan.md`:
- **Exists:** Read it, resume from last incomplete step.
- **Doesn't exist:** Create from template (see below), start at Phase 1.

### 3. Route to Current Phase

Check artifacts on disk AND plan.md status. If they disagree, trust the artifact.

```
Phase 1: Grilling
  → Check: docs/kickoff/v<N>/requirements.md exists?
    → No: invoke /grilling (output dir: docs/kickoff/v<N>/)
    → Yes: check coverage table — any Outstanding items?
      → Outstanding: resume grilling
      → All Resolved/Deferred: ✅ Phase 1 complete → Phase 2

Phase 2: Research (two tracks, either order)
  → Check: docs/kickoff/v<N>/research-repos.md exists?
    → No: invoke /github-research (output dir: docs/kickoff/v<N>/)
  → Check: docs/kickoff/v<N>/research-landscape.md exists?
    → No: invoke /deep-research (output dir: docs/kickoff/v<N>/)
  → Both exist: ✅ Phase 2 complete → Design Review → check for feedback loop

  Feedback loop: if research-landscape.md contains "⚠️ REQUIREMENT GAP":
    → Notify user: "Research found gaps in requirements. Review and decide:"
      → Option A: Loop back to Phase 1 (update requirements.md)
      → Option B: Acknowledge and proceed (gaps become known risks)

Phase 2.5: Design Review (post-research gate)
  → Cross-check all Phase 1 grilling decisions against Phase 2 research findings
  → For each grilling answer that conflicts with a research finding, present:
    "Q[N] says [X], but research found [Y]. Resolve?"
  → Create docs/kickoff/v<N>/design-review-changes.md with this format:
    | # | Grilling Answer | Research Finding | Resolution | Rationale |
    Resolution values: accepted (keep grilling answer), changed (adopt research), deferred
  → Gate: all conflicts resolved (accepted, changed, or deferred)
  → This step prevents the spec from silently diverging from research

Phase 3: Architecture
  → Check: docs/kickoff/v<N>/architecture.md exists?
    → No: invoke /architecture-extraction (output dir: docs/kickoff/v<N>/)
    → Yes: check for accepted ADRs
      → No accepted ADRs: resume architecture review
      → At least 1 accepted: ✅ Phase 3 complete

Phase 4: Spec Publish
  → Invoke /spec with source: docs/kickoff/v<N>/
  → /spec synthesizes requirements + architecture into a design spec
  → Output: docs/phoenix-development-workflow/specs/YYYY-MM-DD-slug-design.md
  → Gate: spec file exists in specs/

Done:
  → "Kickoff complete. Spec published to docs/phoenix-development-workflow/specs/."
  → "Next: run /project-setup (if first time) then /dev to start building."
  → Update plan.md — all steps checked
```

### 4. Update State After Each Phase

After each phase completes, update `docs/kickoff/v<N>/plan.md`:
- Check off the completed step
- Update `current_phase` in frontmatter
- Update `last_updated` timestamp

## Hard Rules

1. **Never skip grilling** — even for "simple" projects. Simple projects generate the most wasted work from unexamined assumptions.
2. **Research before architecture** — architecture decisions must cite findings because evidence-based design prevents building on assumptions that turn out to be wrong.
3. **Design review after research** — cross-check grilling decisions against research before proceeding to architecture. This catches conflicts (e.g., choosing an API that doesn't support a required feature) before they're baked into ADRs.
4. **User confirms gate passage** — auto-advancing removes the user's opportunity to review artifacts and course-correct between phases.
5. **Feedback loop** — if research flags new requirements, offer to loop back to grilling.
6. **User reviews the spec before publishing** — the spec is the contract between kickoff and dev, and publishing without review locks in decisions the user hasn't validated.
7. **Never skip phases to jump to implementation** — kickoff produces artifacts that the dev workflow consumes. Skipping artifact production breaks the handoff contract. Implementation planning belongs to `/dev` → `/plan-phase`, not kickoff.
8. **Phase skips require explicit user approval** — if the user says "skip [phase]" with a stated reason, document the skip in `plan.md` under a "Skipped Phases" section with the user's rationale, then advance. The audit trail matters more than rigid enforcement.

## State File Template

**File:** `docs/kickoff/v<N>/plan.md`

```markdown
---
version: v<N>
current_phase: grilling
started: [date]
last_updated: [date]
blocker: none
spec_published: false
---
# Kickoff Plan — [Project Name] (v<N>)

## Steps
- [ ] Phase 1: Grilling → `docs/kickoff/v<N>/requirements.md`
- [ ] Phase 2A: GitHub Research → `docs/kickoff/v<N>/research-repos.md`
- [ ] Phase 2B: Web Research → `docs/kickoff/v<N>/research-landscape.md`
- [ ] Phase 2.5: Design Review → `docs/kickoff/v<N>/design-review-changes.md`
- [ ] Phase 3: Architecture → `docs/kickoff/v<N>/architecture.md`
- [ ] Phase 4: Spec Publish → `docs/phoenix-development-workflow/specs/YYYY-MM-DD-*-design.md`

## Notes
[Any session notes, decisions, or context for resumption]
```

## Artifact Map

| Phase | Skill | Output | Location |
|-------|-------|--------|----------|
| 1 | grilling | Requirements | `docs/kickoff/v<N>/requirements.md` |
| 2A | github-research | Repo analysis | `docs/kickoff/v<N>/research-repos.md` |
| 2B | deep-research | Landscape research | `docs/kickoff/v<N>/research-landscape.md` |
| 2.5 | (inline) | Design review — cross-check grilling vs research | `docs/kickoff/v<N>/design-review-changes.md` |
| 3 | architecture-extraction | Architecture decisions | `docs/kickoff/v<N>/architecture.md` |
| 4 | spec | Published design spec | `docs/phoenix-development-workflow/specs/YYYY-MM-DD-slug-design.md` |
| — | kickoff | State tracker | `docs/kickoff/v<N>/plan.md` |

## Gates

These mirror the gate conditions in the frontmatter `steps` block (machine-readable) and the routing logic above.

- [ ] Phase 1 gate: requirements.md exists, all categories Resolved/Deferred
- [ ] Phase 2 gate: both research-repos.md and research-landscape.md exist
- [ ] Phase 2.5 gate: design-review-changes.md exists, all conflicts resolved
- [ ] Phase 3 gate: architecture.md exists with at least 1 accepted ADR
- [ ] Phase 4 gate: spec file exists in `docs/phoenix-development-workflow/specs/`

## Anti-Patterns

- Hand off to the dev workflow for coding rather than starting from this orchestrator — kickoff produces specs, not code, and mixing concerns leads to unplanned implementation.
- Follow the phase order (except 2A/2B which can run either way) — each phase builds on the previous phase's output, and skipping creates gaps in evidence.
- Verify artifacts exist on disk before marking gates as passed — plan.md checkboxes without matching files create false progress that breaks resumability.
- Investigate when plan.md and artifacts disagree rather than proceeding — disagreements mean either the plan is stale or an artifact was deleted, both of which need resolution.
- Get user confirmation before publishing a spec — the spec is the contract between kickoff and dev, and publishing without review locks in decisions the user hasn't validated.
