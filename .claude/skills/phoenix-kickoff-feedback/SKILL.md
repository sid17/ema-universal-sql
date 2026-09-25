---
name: phoenix-kickoff-feedback
description: "Capture process reflection after a kickoff workflow run."
when_to_use: "After completing a /kickoff run. User must explicitly invoke."
trigger: /phoenix-kickoff-feedback
---

# Phoenix Kickoff Feedback

Capture a structured process reflection after a `/kickoff` run. This generates a `meta.md` artifact that can be processed via `/skillsmanager-improve` in phoenix-skills.

---

## How to Run

1. User invokes `/phoenix-kickoff-feedback`
2. Determine the run ID from `docs/kickoff/` (e.g., `v1`, `v2`)
3. Read all artifacts from that run to ground the reflection
4. Generate the feedback artifact

---

## Output

Write to: `meta/phoenix-kickoff/<run-id>/meta.md`

---

## Feedback Questions by Phase

### Phase 1: Grilling (Requirements Elicitation)
- Did the 10-category structure surface requirements you wouldn't have thought of on your own?
- Were any categories irrelevant or missing for this project type?
- Did the MC format with recommendations speed up or slow down decision-making?
- Were deferred items appropriately deferred, or should they have been resolved?

### Phase 2A: GitHub Research
- Did the discovered repos provide genuinely useful implementation patterns?
- Were the search queries well-targeted, or did they miss relevant projects?
- Was the analysis depth appropriate (too shallow / too deep)?

### Phase 2B: Deep Research (Landscape)
- Did the landscape research surface information not found in GitHub repos?
- Were the sources credible and current?
- Did research findings cause any requirement gaps to surface (feedback loop)?

### Phase 3: Architecture Extraction
- Did the ADRs cite specific research findings as evidence?
- Were the architectural decisions well-scoped (not too broad, not too granular)?
- Did any ADR feel premature or under-supported by evidence?

### Phase 4: Spec Publication
- Did the final spec accurately synthesize requirements + architecture?
- Was the spec actionable enough for the dev workflow to consume?
- Were any important decisions or context lost in the synthesis?

### Cross-Phase
- Was the pacing appropriate (too fast through any phase, too slow)?
- Did the gates between phases catch real issues or just add ceremony?
- Was the feedback loop (research → grilling) triggered when it should have been?
- Did the orchestrator route to the correct phase on resume?

---

## Output Template

```markdown
# Meta — Process Reflection & Kickoff Methodology

## The Problem We Were Solving
One paragraph: what was the challenge this kickoff run addressed?

## The Process We Followed

### Phase 1: Grilling (Structured Requirements Elicitation)
**What we did:** numbered list of concrete actions taken
**Output:** list of artifacts produced (with paths)
**Process insight:** what worked, what implicit pattern made it effective

### Phase 2A: GitHub Research (Open-Source Discovery)
**What we did:** ...
**Output:** ...
**Process insight:** ...

### Phase 2B: Deep Research (Landscape Analysis)
**What we did:** ...
**Output:** ...
**Process insight:** ...

### Phase 3: Architecture Extraction (Evidence-Based ADRs)
**What we did:** ...
**Output:** ...
**Process insight:** ...

### Phase 4: Spec Publication (Synthesis & Handoff)
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
| Grilling | Structured elicitation across categories | ... |
| GitHub Research | Discover prior art in open source | ... |
| Deep Research | Landscape scan beyond code | ... |
| Architecture | Evidence-based decision records | ... |
| Spec | Synthesize into actionable spec | ... |

## Session History
| Session | Date | What Happened | Key Output |
|---------|------|---------------|------------|
| ... | ... | ... | ... |
```
