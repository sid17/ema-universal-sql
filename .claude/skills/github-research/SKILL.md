---
name: github-research
description: "Use when the user has requirements and wants to know what open-source implementations exist. Trigger whenever someone says 'find repos', 'what's on GitHub', 'look at open source', 'research implementations', or 'what exists in the wild' — even if they don't mention GitHub explicitly. Also trigger when architecture-extraction needs prior art."
when_to_use: "When you have requirements and need to understand what implementations exist in the open-source world"
domain: core
category: investigation
inputs: "docs/kickoff/requirements.md"
outputs: "docs/kickoff/research-repos.md — per-repo analysis cards + comparison table"
handoff: "architecture-extraction"
---

# GitHub Research — Repo Discovery & Analysis Pipeline

## Context

Discovers what exists in the open-source world that's relevant to a project's requirements. Uses `gh` CLI for search and metadata, `repomix` for code analysis, and Claude for pattern extraction. Produces a structured comparison of approaches found in the wild.

**Dependencies:** `gh` CLI (pre-installed), `repomix` (`npm i -g repomix`)

## Instructions

### Step 1: Generate Search Keywords

Read `docs/kickoff/requirements.md`. Generate 5-10 search keywords that capture:
- The core problem domain
- Key technical approaches mentioned in requirements
- Alternative phrasings and related tools

### Step 2: Discover Repos

For each keyword:
```bash
gh search repos "<keyword>" --sort stars --limit 10
```

Collect all results into a candidate list. Deduplicate repos that appear for multiple keywords.

### Step 3: Triage

For each candidate repo, fetch metadata:
```bash
gh repo view <owner>/<repo> --json name,description,stargazerCount,updatedAt,primaryLanguage
```

Filter to top 5-8 repos by:
- **Relevance** — does it solve a similar problem?
- **Stars** — credibility signal (prefer >100 stars)
- **Recency** — updated within last 12 months
- **Language fit** — compatible with project's tech stack

### Step 4: Analyze (Adaptive Depth)

**For top 3-5 repos** (most relevant): Pack and deep analyze:
```bash
repomix --remote <github-url> --style markdown --compress
```
Then analyze: architecture, key decisions, patterns, trade-offs, relevance to our project.

**For remaining repos**: Quick scan via README + file tree (already fetched in triage). Extract high-level approach only.

### Step 4b: Reference Implementation Check

If any analyzed repo is a **close match** to what you're building (same problem domain, similar architecture, 1000+ stars), flag it as a reference implementation:

> "**Reference implementation found:** [repo-name] closely matches our project. Read `references/building-from-reference.md` for how to clone, track, and build from this reference through spec and plan phases."

Add a `## Reference Implementation` section to the output with clone instructions and the key patterns to adopt.

### Step 5: Curate & Compare

Build a side-by-side comparison table across the analyzed repos. Score each on dimensions relevant to the project. Add confidence scoring:
- **High (90%+):** Read full source, architecture is clear
- **Medium (70-89%):** Read README + key files, inferred architecture
- **Low (50-69%):** README only, limited understanding

### Step 6: Write Output

Write `docs/kickoff/research-repos.md`.

## Gates

- [ ] At least 3 repos analyzed (deep or quick scan)
- [ ] `docs/kickoff/research-repos.md` exists with comparison table
- [ ] Each deep-analyzed repo has architecture patterns extracted

## Anti-Patterns

- Skip repos with 0 stars unless they solve a unique problem — zero-star repos lack community validation and cost analysis time that's better spent on proven options.
- Triage first, repomix only finalists — packing irrelevant repos wastes tokens and dilutes the analysis with noise.
- Only extract architectural patterns that are actually in the code — inventing patterns misleads architecture decisions with fictional evidence.
- Always state a repo's relevance to the specific project requirements — recommendations without context force the user to re-evaluate every repo themselves.

## Output Format

**File:** `docs/kickoff/research-repos.md`

```markdown
# GitHub Research — [Project Name]

## Search Keywords
[list of keywords used]

## Repos Analyzed

### <repo-name> (⭐ N stars)
- **What it does:** [1-2 sentences]
- **Architecture:** [key structural decisions]
- **Key patterns:** [what's reusable]
- **Trade-offs:** [what they chose and what they gave up]
- **Relevance to us:** [specific connection to our requirements]
- **Confidence:** High / Medium / Low

(repeat for each repo)

## Comparison Table
| Dimension | Repo A | Repo B | Repo C |
|-----------|--------|--------|--------|

## Patterns to Adopt
- [pattern] — from [repo], because [rationale]

## Patterns to Skip
- [pattern] — from [repo], because [rationale]

## Next Step
→ Phase 3: Architecture (run /kickoff to continue)
```
