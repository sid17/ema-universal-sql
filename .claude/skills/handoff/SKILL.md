---
name: handoff
description: "Use at session end, after phase completion, or when checkpointing progress. Trigger whenever the user says 'handoff', 'wrap up', 'stopping for now', 'save progress', 'end session', or mentions resuming later, even if they don't explicitly say 'handoff'."
when_to_use: "Session is ending, phase just completed, or user wants to checkpoint progress for continuity."
domain: dev
category: orchestration
inputs: "Git log, plan files with checkboxes, user input on failed approaches"
outputs: "docs/phoenix-development-workflow/handoff.md — synthesized session state for next session"
handoff: "none"
argument-hint: "[checkpoint|full]"
---

# /handoff — Session State Capture

Capture session progress into a single self-contained handoff document. The next session reads this one file and has everything needed to resume.

## Context

HANDOFF.md is the only persistent state file in the dev workflow. It replaces PROGRESS.md, BRIEFING.md, and WORKING-CONTEXT.md — one file, always current, written at natural breakpoints. It lives at `docs/phoenix-development-workflow/handoff.md` and is overwritten each time.

**Checkpoint vs full handoff:** If unchecked phases remain, frame as a checkpoint ("Phases 0-2 done, Phase 3 next"). If all phases are done or the user is explicitly ending, frame as a full session handoff.

## Instructions

### Step 1: Detect Session Commits

Find commits made during this session:
1. If `docs/phoenix-development-workflow/handoff.md` exists, read it. Find the last commit SHA from the Plan Status table's "Commits" column. Run `git log <last-sha>..HEAD --oneline`.
2. If no previous HANDOFF.md exists (first session), run `git log --since="8 hours ago" --oneline`.
3. Save the commit list for the "What Was Done" section.

### Step 2: Scan Plan Status

Scan all files in `docs/phoenix-development-workflow/plans/*.md`:
- For each plan file, count `- [x]` (checked) vs `- [ ]` (unchecked) tasks
- **Done:** all tasks checked
- **In progress:** some tasks checked
- **Not started:** no tasks checked
- Build the Plan Status table with phase name, status, and associated commit SHAs
- If no plan files exist yet, note "No plans created yet" in the status table

### Step 3: Prompt for Failed Approaches

Ask the user: **"Were there any failed approaches or dead ends worth noting for the next session?"**
- Do NOT rely on agent recall — context may have compressed early failures
- User can say "none" — this section is optional
- If the user provides items, include them in "What Didn't Work" with the reason

### Step 4: Generate HANDOFF.md

Write `docs/phoenix-development-workflow/handoff.md` using the v2 format:

```
# Handoff

> Last updated: [today's date] (Session [N — count previous HANDOFF.md overwrites from git log, or 1 if first])

## Project
[1-2 lines from CLAUDE.md or user context]

## Plan Status
[Table from Step 2]

## What Was Done This Session
[Commit list from Step 1, with brief descriptions]

## What Didn't Work
[From Step 3, or "None"]

## What's Next
- [Specific next action — if all phases done, note post-dev tasks: deploy, docs, cleanup]
- Plan file: [path to current/next plan file, or "All plans complete"]
- Last completed: [Task ID or phase]
- Next: [Task ID, "Start Phase N", or "Project complete — see post-dev tasks above"]

## Key Decisions
[Only decisions that affect upcoming work]

## Watch-outs
[Things that will bite the next session]

## Open Questions
[Unresolved items needing user input]

## Key Files
[Critical files with why they matter]
```

### Step 5: User Review

Display the generated HANDOFF.md to the user. Ask: **"Review the handoff — any changes before I commit?"**
- User can edit, add context, or approve as-is
- Apply any requested changes

### Step 6: Commit

After user confirms, commit HANDOFF.md: `git add docs/phoenix-development-workflow/handoff.md && git commit -m "session handoff: [brief summary]"`

## Gates

- [ ] Git log scanned for session commits
- [ ] Plan files scanned for checkbox status
- [ ] User reviewed and confirmed HANDOFF.md before commit

## Anti-Patterns

- Always ask the user about failed approaches rather than relying on agent memory — context compression may have dropped early failures that the user remembers.
- Get user confirmation before committing — the user may want to adjust the handoff before it becomes the permanent record.
- Synthesize for the next session's needs rather than writing raw session notes — raw notes force the next session to re-interpret everything from scratch.
- Only include decisions that affect upcoming work — stale decisions clutter the handoff and waste the next session's attention.

## Output Format

**File:** `docs/phoenix-development-workflow/handoff.md` — 8-section synthesized handoff document
