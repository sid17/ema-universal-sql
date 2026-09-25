---
name: architecture-extraction
description: "Use after requirements and research are complete to make architecture decisions before building. Trigger whenever the user says 'architecture', 'tech stack', 'ADR', 'system design', 'what should we build with', or 'how should this be structured' — even if they don't mention architecture explicitly. Also trigger when kickoff reaches Phase 3."
when_to_use: "When requirements and research are complete, and you need to finalize architecture before implementation"
domain: core
category: generation
inputs: "docs/kickoff/requirements.md, docs/kickoff/research-repos.md, docs/kickoff/research-landscape.md"
outputs: "docs/kickoff/architecture.md — system overview, ADR decisions citing research, tech stack, risks"
handoff: "none — kickoff complete, ready to build"
---

# Architecture Extraction — Research-Backed Architecture Decisions

## Context

Takes sharpened requirements + research findings and produces architecture candidates with trade-offs. Every decision cites Phase 2 research — no designing from imagination. The user makes final calls; this skill presents options grounded in evidence.

## Instructions

### Step 1: Synthesize Inputs

Read all three input files:
- `docs/kickoff/requirements.md` — what we're building and why
- `docs/kickoff/research-repos.md` — how others built similar things
- `docs/kickoff/research-landscape.md` — what the broader landscape says

Identify the key architecture decisions that need to be made. Common decisions include:
- Overall system architecture (monolith, microservices, serverless, etc.)
- Tech stack (language, framework, database, hosting)
- Data model and storage approach
- API design and contracts
- Authentication and authorization approach
- Deployment and infrastructure

### Step 2: Generate Architecture Candidates

For each key decision, generate 2-3 candidates. Each candidate MUST:
- Cite at least one source from research (repo X uses approach Y, article Z recommends...)
- Novel proposals (not from research) are allowed but must be flagged as `[novel — not from research]`

**Data-level ADRs:** When the feature involves external data sources (SDKs, APIs, file formats, databases), include at least one ADR that addresses data contracts: field names, formats, sources, and how to verify them. "Where does session_id come from?" is an architectural decision, not an implementation detail. These decisions prevent the spec from being abstract about data, which causes implementation guesses that become bugs.

### Step 3: Present in ADR Format

For each decision, write a research-backed ADR:

```markdown
## Decision: [What]

### Context
[Problem + constraints from requirements.md]

### Options Considered
| Option | Source | Pros | Cons |
|--------|--------|------|------|
| A: [approach] | [Repo X / Article Y] | ... | ... |
| B: [approach] | [Repo Z] | ... | ... |
| C: [approach] | [novel] | ... | ... |

### Recommendation
[Which option and why — cite research]

### Trade-offs
| Dimension | Rating (1-5) | Notes |
|-----------|-------------|-------|
| Complexity | | |
| Scalability | | |
| Time-to-build | | |
| Proven-ness | | |

### Consequences
- **Positive:** [what we gain]
- **Negative:** [what we give up]
- **Risks:** [what could go wrong]
```

### Step 4: System Overview

Create a high-level system diagram using Mermaid syntax showing the major components and their relationships. Keep it to one diagram — this is an overview, not a detailed design.

### Step 5: User Reviews & Decides

Present all ADRs with recommendations. The user picks the final approach for each decision. Update the ADR status:
- `proposed` → `accepted` (user confirmed)
- `proposed` → `rejected` (user chose different option)

If the user wants more research on a specific option, flag it for a research loop.

### Step 6: Write Output

Write `docs/kickoff/architecture.md` with all decisions finalized.

## Gates

- [ ] At least 2 key architecture decisions documented in ADR format
- [ ] Every ADR has at least one option citing Phase 2 research
- [ ] User has confirmed decisions (status = accepted)
- [ ] `docs/kickoff/architecture.md` exists with system overview + ADRs

## Anti-Patterns

- Cite research for every architecture decision — the value of this skill over freehand design is that every choice has evidence behind it, not just intuition.
- Always show alternatives with trade-offs rather than presenting a single "obvious choice" — single-option presentations bypass the user's judgment and hide trade-offs that matter later.
- Keep PRD content (goals, metrics, timeline, user stories) out of architecture docs — mixing concerns makes both documents harder to maintain and review.
- Present options and let the user confirm — auto-accepting removes the user's agency over decisions that shape the entire project.
- Keep architecture aligned with requirements — contradicting the requirements means either the requirements or the architecture is wrong, and that conflict needs to be surfaced, not buried.

## Output Format

**File:** `docs/kickoff/architecture.md`

```markdown
# Architecture — [Project Name]

## System Overview
[Mermaid diagram]

## Key Decisions

### ADR-001: [Decision Title]
(full ADR format as above)

### ADR-002: [Decision Title]
...

## Tech Stack Summary
| Component | Choice | Rationale |
|-----------|--------|-----------|

## Data Model
[Key entities and relationships — if applicable]

## Risks & Mitigations
| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|

## Status
Kickoff complete. Ready to build.
```
