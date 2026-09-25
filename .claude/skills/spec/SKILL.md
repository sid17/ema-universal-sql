---
name: spec
description: "Use after kickoff Phase 3 completes to publish a design spec, or standalone when you already have requirements and architecture docs and need a spec before development. Trigger whenever the user says 'write a spec', 'publish the spec', 'create a design doc', or 'I'm ready to build' after kickoff, even if they don't mention '/spec' explicitly."
when_to_use: "After kickoff Phase 3 completes, or standalone when you have requirements + architecture and need a spec"
domain: core
category: synthesis
trigger: /spec
inputs: "requirements.md + architecture.md from a kickoff version directory"
outputs: "docs/phoenix-development-workflow/specs/YYYY-MM-DD-slug-design.md; docs/kickoff/v<N>/plan.md (updated when called from /kickoff)"
---

# /spec — Design Spec Publisher

## Context

The bridge between kickoff (brainstorming) and dev (building). Reads the raw kickoff artifacts — requirements and architecture — and synthesizes them into a single, self-contained design spec that plan-phase can consume.

**When to use:** After kickoff completes (Phase 4 calls this automatically), or standalone when adding a feature to an existing project.
**When NOT to use:** If you don't have requirements or architecture yet — run kickoff first.

## On Invocation

### 1. Locate Source Artifacts

Check in this order:
1. If called from `/kickoff` with a version: use `docs/kickoff/v<N>/`
2. If called standalone with an argument (`/spec v2`): use `docs/kickoff/v2/`
3. If called standalone with no argument: scan `docs/kickoff/` for the latest version directory

**Required files:**
- `requirements.md` — must exist (the PRD from grilling)
- `architecture.md` — must exist (ADRs from architecture-extraction)

**Optional files (enrich the spec if present):**
- `research-repos.md` — cite key findings
- `research-landscape.md` — cite key findings

If either required file is missing, stop and tell the user: "Missing [file]. Run /kickoff to complete Phase [N] first."

### 2. Ask for Spec Metadata

Ask the user:
- **Feature name** (short slug, e.g., "workspace-ui-mvp"): used in filename
- **Publish date** (default: today): used in filename prefix

### 3. Synthesize the Spec

Read all source artifacts and produce a single design spec with these sections:

```markdown
# [Feature Name] — Design Spec

## Reference Alignment (if research found a reference implementation)
### Same as [reference-name]:
- [pattern we're adopting]
### Different from [reference-name]:
- [pattern we're changing] — **Why:** [justification]

## Context
[What you're building and why — synthesized from requirements.md intro/goals]

## Architecture
[Directory structure, data flow, key components — from architecture.md]

## Tech Stack + Key Decisions
| Decision | Pick | Why |
[Table of architectural decisions — from architecture.md ADRs]

## Data Contracts (required when spec involves external data)
[Paste-actual-output examples with verification dates — not descriptions.
Show the real JSON, SQL row, file content, or SDK output.
Include verification method: `head -1 path/to/file`, `curl /api/endpoint`, etc.]

## Data Format / Schema
[Schema overview — from requirements.md technical constraints + architecture.md]

## User Flows
[Primary "what happens when..." scenarios tracing cross-cutting concerns.
Not edge cases — the main flows users will exercise:
- Send → Refresh → Resume
- Create → Switch → Return
- Error → Recovery → Retry
Each flow should trace through the full system, not stop at one component.]

## Features by Phase
| Phase | Name | What It Delivers |
[Scope breakdown — from requirements.md feature list, organized into buildable phases]

## Risks + Mitigations
| Risk | Mitigation |
[From architecture.md risk sections + requirements.md deferred items]

## Source Artifacts
- Requirements: `docs/kickoff/v<N>/requirements.md`
- Architecture: `docs/kickoff/v<N>/architecture.md`
- Research: `docs/kickoff/v<N>/research-repos.md`, `docs/kickoff/v<N>/research-landscape.md`
```

**Rules for synthesis:**
- The spec must be **self-contained** — a reader should not need to open the source artifacts to understand what to build
- Cite research findings where they informed a decision (e.g., "Chose chokidar based on Phase 2A research: 30K+ stars, cross-platform, handles recursive watching")
- Keep it under 300 lines — this is a spec, not a book
- Features by Phase should be scope only (what), not tasks (how) — plan-phase generates the tasks
- **If research found a reference implementation:** start the spec with a "Reference Alignment" section containing "Same as / Different from" lists. Every divergence from the reference needs a "Why." This prevents the spec from silently diverging from proven patterns that research already validated.

### 4. Present for Review

Show the synthesized spec to the user. **Do not write the file until the user approves.**

Ask: "Ready to publish this spec? I'll save it to `docs/phoenix-development-workflow/specs/YYYY-MM-DD-slug-design.md`."

### 5. Publish

Write the spec to: `docs/phoenix-development-workflow/specs/YYYY-MM-DD-slug-design.md`

Create the `specs/` directory if it doesn't exist.

If called from `/kickoff`, update `docs/kickoff/v<N>/plan.md`:
- Check off Phase 4
- Set `spec_published: true`
- Add the spec path to Notes

## Gates

- [ ] Both requirements.md and architecture.md exist in the source directory
- [ ] User has reviewed and approved the synthesized spec
- [ ] Spec file written to `docs/phoenix-development-workflow/specs/`
- [ ] If called from /kickoff: plan.md updated with Phase 4 checked off and spec path noted

## Anti-Patterns

- Synthesize and consolidate rather than copy-pasting from source artifacts — a reader should understand the full picture from the spec alone without flipping between files.
- Leave implementation tasks to plan-phase — the spec defines what to build, not how to build it step by step.
- Get user approval before writing the spec file — the spec is the contract and writing it without review bypasses the user's authority over scope.
- Cite key findings and link to source artifacts rather than including research verbatim — the spec should be concise, not a research dump.
- Each spec gets a unique date + slug filename rather than overwriting existing ones — specs are the contract record and overwriting loses the decision trail.
