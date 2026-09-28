# Phoenix Software Workflow

> The full development lifecycle: idea to shipped code. Kickoff refines your idea into a spec, dev builds it.

---

## How It Works (for humans)

```
Idea → /kickoff → Spec → /dev → Shipped Code
                    ↑                    |
                    └── next feature ────┘
```

### The Steps

1. **Have an idea** — even a one-liner is fine
2. **Run `/kickoff`** — walks you through:
   - Grilling: structured Q&A to define requirements
   - Research: finds similar projects, analyzes the landscape
   - Architecture: evidence-based technical decisions
   - Spec: synthesizes everything into a design spec
3. **Spec lands in `docs/phoenix-development-workflow/specs/`**
4. **Run `/project-setup`** (first time only) — configures project conventions
5. **Run `/dev`** — picks up the spec and:
   - Plans: decomposes spec into tasks (you review and approve)
   - Builds: executes tasks with verification
   - Reviews: code review + security review
   - Deploys: when you're ready
6. **For the next feature** — run `/kickoff v2`, get a new spec, run `/dev`

### Quick Reference

| I want to... | Run |
|-------------|-----|
| Start a new project from an idea | `/kickoff` |
| Add a major feature | `/kickoff v2` |
| Build from a spec | `/dev` |
| Resume where I left off | `/dev` |
| Check project status | `/dev status` |
| Save session state | `/dev handoff` |
| Give process feedback | `/phoenix-kickoff-feedback` or `/phoenix-development-feedback` |

---

<!-- ============================================================
     BELOW THIS LINE: Agent-facing details.
     The human section above is the cheat sheet.
     Everything below helps agents route correctly and understand
     what's installed, where things live, and how the workflows connect.
     ============================================================ -->

## Workflow Routing (for agents)

Pick the workflow that matches the current task. Only load the docs for the workflow you need.

| If you're... | Use | Entry point |
|-------------|-----|-------------|
| Refining requirements, researching, making architecture decisions | **Kickoff** | `docs/phoenix-kickoff-workflow/kickoff-workflow.md` |
| Building features, writing code, running tests, deploying | **Development** | `docs/phoenix-development-workflow/dev-workflow.md` |

---

## Installed Skills

### Kickoff Workflow

| Skill | Invoke | Phase | What It Does |
|-------|--------|-------|-------------|
| kickoff | `/kickoff` | — | Orchestrator — checks state, routes to correct phase |
| grilling | `/grilling` | 1 | Structured requirements elicitation (10 categories) |
| github-research | `/github-research` | 2A | Discover + analyze open-source implementations |
| deep-research | `/deep-research` | 2B | Landscape research — articles, benchmarks, expert opinions |
| architecture-extraction | `/architecture-extraction` | 3 | Evidence-based ADRs citing Phase 2 research |
| spec | `/spec` | 4 | Synthesize requirements + architecture into a published design spec |

### Development Workflow

| Skill | Invoke | What It Does |
|-------|--------|-------------|
| dev | `/dev` | Orchestrator — reads state, routes to correct phase |
| plan-phase | `/plan-phase` | Reads spec → creates milestone plan with checkboxes |
| build-phase | `/build-phase` | Executes plan tasks, embedded verify, emits REVIEW.md |
| debug | `/debug` | 4-phase systematic debugging with escalation |
| verify | `/verify` | Iron Law — no task done without evidence |
| code-review | `/code-review` | Two-stage: scope drift check + code quality |
| gen-tests | `/gen-tests` | Generate tests for new/changed code |
| security-review | `/security-review` | OWASP-style security checklist |
| deploy | `/deploy` | Config-driven deployment with health checks |
| handoff | `/handoff` | Capture session state for next session continuity |
| project-setup | `/project-setup` | Generate CLAUDE.md + DESIGN.md from stack questions |

---

## The Handoff: Kickoff → Dev

The handoff point is `docs/phoenix-development-workflow/specs/`.

- `/kickoff` Phase 4 (`/spec`) publishes a self-contained design spec here
- `/dev` state check scans this directory for specs
- `/plan-phase` reads the latest spec by date and decomposes it into tasks

No manual copy step. The spec directory is pre-created during install.

### Versioning

Each kickoff run is versioned (`docs/kickoff/v1/`, `v2/`, ...). Each version publishes a spec with a unique date-slug filename. Previous specs and plans remain untouched. Plan dates match their source spec dates for traceability.

---

## Key Paths

| Path | Purpose |
|------|---------|
| `docs/phoenix-kickoff-workflow/` | Kickoff workflow docs (agent handoff, grilling guide) |
| `docs/phoenix-development-workflow/` | Dev workflow docs (agent handoff, lifecycle, getting-started) |
| `docs/phoenix-development-workflow/specs/` | Design specs — the bridge between kickoff and dev |
| `docs/phoenix-development-workflow/plans/` | Execution plans created by plan-phase |
| `docs/phoenix-development-workflow/handoff.md` | Session state — created by `/handoff` |
| `docs/kickoff/v<N>/` | Kickoff brainstorming artifacts per version |

---

## Rules and Hooks

- **Rules:** `.claude/rules/code-quality.md` — always-on code quality constraints
- **Hooks:** `.claude/settings.json` — safety hooks (block destructive commands, secrets, config weakening) + stack-specific formatting/testing
- See individual workflow docs for hook details
