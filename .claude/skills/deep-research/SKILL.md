---
name: deep-research
description: "Use when a project has defined requirements and needs landscape research before architecture decisions. Trigger whenever the user asks to research approaches, find what tools exist, understand the competitive landscape, explore best practices, or investigate what experts recommend — even if they just say 'what's out there' or 'how do others solve this'."
when_to_use: "When you have requirements and need to understand the broader landscape — articles, tools, approaches, expert opinions"
domain: core
category: investigation
inputs: "docs/kickoff/requirements.md"
outputs: "docs/kickoff/research-landscape.md — findings by topic, resources list, flagged new requirements"
handoff: "architecture-extraction"
---

# Deep Research — Automated Web Landscape Research

## Context

Researches the broader landscape around a project's requirements — what approaches exist, what's proven, what's emerging, what experts recommend. Unlike github-research (which analyzes code), deep-research covers articles, blog posts, documentation, discussions, and tools. Executes research directly via WebSearch + WebFetch (no manual steps).

## Instructions

### Step 1: Generate Sub-Queries

Read `docs/kickoff/requirements.md`. Decompose the research need into 5-10 focused sub-queries that cover:
- The core problem space ("how do teams solve X")
- Alternative approaches ("X vs Y vs Z comparison")
- Best practices ("best practices for X in 2025/2026")
- Tools and services ("tools for X")
- Failure modes ("common mistakes when building X")

Prioritize sub-queries by `Impact × Uncertainty` — research what matters most and what we know least about.

### Step 2: Broad Scan (Pass 1)

For each sub-query:
1. Run `WebSearch` with the query
2. Scan results — identify the 2-3 most relevant URLs per query
3. Run `WebFetch` on the top URLs to extract key insights
4. Record: source, key finding, relevance to our requirements

Target: 15-25 sources scanned across all sub-queries.

### Step 3: Deep Dive (Pass 2)

From Pass 1 findings, identify the top 3 most impactful topics or sources. For each:
1. Generate 2-3 follow-up queries that go deeper
2. Run WebSearch + WebFetch on follow-ups
3. Extract detailed insights — specific approaches, data points, expert opinions

### Step 4: Check for Requirement Gaps

Review findings against `docs/kickoff/requirements.md`. Flag any dimensions that:
- Were NOT covered in requirements but surfaced as important during research
- Contradict assumptions made during grilling
- Suggest the problem space is different than initially understood

Format flags as: `⚠️ REQUIREMENT GAP: [what research found] — not in requirements.md`

If gaps are found, note them in the output. The /kickoff orchestrator will route back to grilling if needed.

### Step 5: Write Output

Compile findings into `docs/kickoff/research-landscape.md`.

## Gates

- [ ] At least 10 sources consulted
- [ ] `docs/kickoff/research-landscape.md` exists with findings and resources list
- [ ] Requirement gap check completed (gaps flagged or "no gaps found")

## Anti-Patterns

- Execute research directly via WebSearch/WebFetch rather than generating a prompt for the user — the value of this skill is automated execution, not delegation.
- Demand named examples, specific data, and real tools — generic advice ('use a good framework') wastes the user's time because it doesn't inform architecture decisions.
- Stay within the project's requirements scope — researching unrelated topics dilutes focus and wastes tokens on findings that won't influence decisions.
- Only report what WebSearch/WebFetch actually returned — fabricated sources erode trust and lead to design decisions based on nonexistent evidence.

## Output Format

**File:** `docs/kickoff/research-landscape.md`

```markdown
# Landscape Research — [Project Name]

## Sub-Queries Investigated
1. [query] — [why this matters]
...

## Key Findings

### [Topic 1]
- **Finding:** [what we learned]
- **Source:** [URL or reference]
- **Relevance:** [how this connects to our requirements]

### [Topic 2]
...

## Deep Dive Findings

### [Topic from Pass 2]
[Detailed analysis with specific data points, approaches, expert quotes]

## Requirement Gaps Flagged
- ⚠️ [gap description] — not in requirements.md
- (or: ✅ No gaps found — requirements align with research)

## Resources for Further Reading
| Resource | URL | Why It Matters |
|----------|-----|---------------|

## Next Step
→ Phase 3: Architecture (run /kickoff to continue)
```
