# Dev Workflow — Agent Handoff

> This file tells the agent what's installed and how to use it.
> Copied from phoenix-skills during bootstrap. See `guides/development/bootstrap.md` for setup instructions.
> For phase transitions, artifact formats, and session continuity rules, see `lifecycle.md`.

---

## Available Skills

All skills live in `.claude/skills/<skill-name>/SKILL.md`.

| Skill | Invoke | What It Does |
|-------|--------|-------------|
| **dev** | `/dev` | Orchestrator — reads HANDOFF.md + plan state, routes to the correct phase |
| **plan-phase** | `/plan-phase` | Reads PRD/spec → creates a milestone plan with checkboxes |
| **build-phase** | `/build-phase` | Executes plan tasks, embedded verify, emits REVIEW.md for user review |
| **debug** | `/debug` | 4-phase systematic debugging with escalation |
| **verify** | `/verify` | Iron Law — no task is done without evidence (embedded in build-phase + on-demand) |
| **code-review** | `/code-review` | Two-stage review: scope drift check + code quality |
| **gen-tests** | `/gen-tests` | Generate tests for new/changed code |
| **security-review** | `/security-review` | OWASP-style security checklist |
| **deploy** | `/deploy` | Config-driven deployment with health checks |
| **handoff** | `/handoff` | Capture session state for next session continuity |
| **project-setup** | `/project-setup` | Generate CLAUDE.md + DESIGN.md from stack questions |

---

## How to Start

1. **Ensure a spec exists**: Either from `/kickoff` (published to `docs/phoenix-development-workflow/specs/`) or written manually
2. **First time**: Run `/project-setup` to finalize project conventions (generates CLAUDE.md + DESIGN.md)
3. **Then**: Run `/dev` — plan-phase reads the latest spec from `specs/`, creates execution plans, then routes to build
4. The `/dev` orchestrator manages the full cycle: **plan → build → verify → review → deploy**

You can also invoke individual skills directly (e.g., `/debug` when stuck, `/gen-tests` to add tests, `/handoff` to checkpoint progress).

### Related Docs

| Doc | When to read |
|-----|-------------|
| `getting-started.md` | Human's guide — what to do, in what order, and what to type |
| `lifecycle.md` | Phase transitions, artifact formats, session continuity rules |
| `design-system.md` | How to create a project design system before `/project-setup` (UI projects) |
| `project-setup-existing.md` | How to run `/project-setup` on a project that already has code |
| `templates/plan-template.md` | Reference format for plan files created by `/plan-phase` |

---

## Active Hooks

Hooks fire automatically — the agent cannot skip them. Configured in `.claude/settings.json`.

| Hook | Trigger | What It Does |
|------|---------|-------------|
| **Block destructive commands** | PreToolUse (Bash) | Blocks `rm -rf` on non-build targets, `DROP TABLE`, force push, hard reset |
| **Block secrets** | PreToolUse (Write/Edit) | Blocks hardcoded API keys, passwords, tokens |
| **Block config edits** | PreToolUse (Write/Edit) | Blocks agent from weakening linter/compiler configs |
| **Auto-format + type-check** | PostToolUse (Write/Edit) | Runs prettier + `tsc --noEmit` on every TS/TSX edit |
| **File size check** | PreToolUse (git commit) | Blocks commits if any file exceeds 500 lines |
| **Test gate** | PreToolUse (git commit) | Runs `npm test` before commit — must pass |

---

## Always-On Rules

- `.claude/rules/code-quality.md` — 7 code quality laws enforced on every task (file size limits, import-first, etc.)

---

## Session State

| File | Purpose |
|------|---------|
| `docs/phoenix-development-workflow/specs/*.md` | Design specs — what to build (`YYYY-MM-DD-feature-design.md`) |
| `docs/phoenix-development-workflow/plans/*.md` | Phase plan files with checkboxes — created by plan-phase (`YYYY-MM-DD-phaseN-slug.md`) |
| `docs/phoenix-development-workflow/handoff.md` | Session state — created by `/handoff` at session end or phase completion |
| `docs/phoenix-development-workflow/REVIEW.md` | Temporary — created by build-phase for user review, deleted after confirmation |

---

## How the Workflow Flows

```
/dev
 ├── Reads docs/phoenix-development-workflow/handoff.md (where are we?)
 ├── Scans docs/phoenix-development-workflow/plans/ for plan status
 ├── plan-phase → creates execution plan with checkboxes
 ├── [HARD GATE] → user reviews plan before any code runs
 ├── build-phase → executes tasks one at a time
 │    ├── hooks fire on every edit (format, type-check)
 │    ├── debug → called automatically if build gets stuck (3-attempt escalation)
 │    ├── gen-tests → generates tests for new/changed code
 │    ├── verify → embedded as final step (Iron Law: run, read, check, then claim)
 │    └── REVIEW.md → emitted for user review (user confirms before commit)
 ├── [PROMPT] → "Run /handoff to checkpoint, or continue?"
 ├── code-review → scope drift + quality review
 └── deploy → config-driven deployment with health checks

/handoff (at session end or phase completion)
 ├── Reads git log for session commits
 ├── Scans plan files for checkbox status
 ├── Prompts for failed approaches
 └── Writes docs/phoenix-development-workflow/handoff.md
```
