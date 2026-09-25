---
name: project-setup
description: "Use when starting a new project for the first time, after kickoff produces architecture docs. Activate whenever someone says they need to set up a project, configure development conventions, or asks about CLAUDE.md generation — even if they don't mention 'project setup' by name."
when_to_use: "After /kickoff completes and before /dev begins. One-time setup for a new project."
domain: dev
category: generation
inputs: "docs/kickoff/architecture.md + docs/kickoff/requirements.md (from /kickoff)"
outputs: "CLAUDE.md, DESIGN.md (UI projects), docs/phoenix-development-workflow/ directory, hook recommendation"
handoff: "dev"
argument-hint: "[reconfigure]"
---

# Project Setup — Interactive Configuration

## Context

Bridges "what to build" (from /kickoff) with "how to build it" (/dev workflow). Asks the user multiple-choice questions across 4 categories, pre-populating recommendations from architecture.md. Generates project-specific config files that the dev workflow consumes. Runs once per project.

## Instructions

### Step 1: Load Kickoff Context

Read inputs:
- `docs/kickoff/architecture.md` — stack decisions (language, framework, DB, API style)
- `docs/kickoff/requirements.md` — scope, UX, deployment targets
- Existing `CLAUDE.md` or `.claude/context.md` (if any)

From architecture, pre-populate recommended answers. If no architecture.md exists, ask the user to describe their stack directly.

### Step 2: Ask Configuration Questions

Walk through 4 question categories (22 questions total). Ask ONE AT A TIME with MC options and a recommended choice. See `references/question-options.md` for the full question list with options per category:
1. Stack & Architecture (7 Qs)
2. Directory Structure (3 Qs)
3. Design System — UI only (6 Qs)
4. Code Conventions (6 Qs)

### Step 3: Generate Config Files

**A. `CLAUDE.md`** (under 100 lines) — stack table, directory structure, code rules, naming conventions, anti-patterns derived from choices. Must include:
```markdown
## Session State
Start a new session by reading docs/phoenix-development-workflow/handoff.md.
```

**B. `DESIGN.md`** (UI projects only) — fonts, spacing, icons, CSS strategy, breakpoints, anti-patterns.

### Step 4: Create Workflow Directory

Create `docs/phoenix-development-workflow/` with `plans/` subfolder:
```bash
mkdir -p docs/phoenix-development-workflow/plans
```
HANDOFF.md is created by `/handoff` at first session end, not at setup time.

### Step 5: Display Hook Recommendation

Display as concrete copy commands:
- Always: `hooks/base/settings.json`
- Stack-specific: typescript, python, or react-native overlay

## Gates

- [ ] All applicable question categories completed
- [ ] CLAUDE.md generated and under 100 lines

## Anti-Patterns

- Always provide MC options with a recommended choice rather than open-ended questions — MC options produce consistent, comparable answers and reduce cognitive load.
- Complete all question categories before generating CLAUDE.md — partial configuration leads to missing conventions that cause inconsistencies during development.
- Keep design system details in DESIGN.md, not CLAUDE.md — mixing concerns makes both files harder to maintain and bloats CLAUDE.md past its 100-line limit.
- Recommend hooks and let the user install them rather than auto-installing — the user should control what runs in their environment.
- Only include conventions the user confirmed — invented conventions surprise the user during development and undermine trust in the generated config.

## Output Format

**File:** `CLAUDE.md` — project conventions
**File:** `DESIGN.md` — design system (UI projects only)
**Directory:** `docs/phoenix-development-workflow/` with `plans/` subfolder
**Displayed:** Hook installation commands
