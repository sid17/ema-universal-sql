# Kickoff Workflow — Agent Handoff

> This file tells the agent what's installed and how to use it.
> Copied from phoenix-skills during bootstrap. See `guides/kickoff/bootstrap.md` for setup instructions.

---

## Available Skills

All skills live in `.claude/skills/<skill-name>/SKILL.md`.

| Skill | Invoke | Phase | What It Does |
|-------|--------|-------|-------------|
| **kickoff** | `/kickoff` | — | Orchestrator — checks state, routes to correct phase |
| **grilling** | `/grilling` | 1 | Structured requirements elicitation (10 categories, MC format) |
| **github-research** | `/github-research` | 2A | Discover + analyze open-source implementations via `gh search` |
| **deep-research** | `/deep-research` | 2B | Landscape research — articles, benchmarks, expert opinions |
| **architecture-extraction** | `/architecture-extraction` | 3 | Evidence-based ADRs citing Phase 2 research |
| **spec** | `/spec` | 4 | Synthesize requirements + architecture into a published design spec |

---

## How to Start

Run `/kickoff`. The orchestrator manages the full pipeline:

1. **Phase 1**: Grilling — structured questions across 10 categories
2. **Phase 2A**: GitHub research — find relevant repos and implementations
3. **Phase 2B**: Deep research — landscape analysis (requires WebSearch MCP)
4. **Phase 3**: Architecture — generate ADRs backed by research findings
5. **Phase 4**: Spec publish — synthesize into a design spec, publish to `specs/`

You can also invoke individual skills directly (e.g., `/grilling` to re-run requirements, `/spec` to publish a spec standalone).

### Preparation

Read `grilling-guide.md` before your first kickoff — it explains the 10 grilling categories and helps you give better answers during Phase 1.

---

## Versioned Sessions

Kickoff sessions are versioned. Each version is a self-contained brainstorming workspace:

- **v1**: First project kickoff (MVP, initial feature set)
- **v2**: Adding a major feature or rethinking architecture
- **v3+**: Subsequent iterations

Run `/kickoff` for the first time → creates `docs/kickoff/v1/`. Run `/kickoff v2` later → creates `docs/kickoff/v2/`. Each version produces its own spec.

---

## How the Workflow Flows

```
/kickoff
 ├── Determine version (v1, v2, ...)
 │
 ├── Phase 1: /grilling
 │    └── Output: docs/kickoff/v<N>/requirements.md
 │    └── Gate: all 10 categories Resolved or Deferred
 │
 ├── Phase 2A: /github-research  ─┐
 │    └── Output: docs/kickoff/v<N>/research-repos.md   │ (either order)
 ├── Phase 2B: /deep-research    ─┘
 │    └── Output: docs/kickoff/v<N>/research-landscape.md
 │    └── Gate: both files exist + no unresolved gaps
 │
 ├── Phase 3: /architecture-extraction
 │    └── Output: docs/kickoff/v<N>/architecture.md
 │    └── Gate: at least 1 accepted ADR
 │
 └── Phase 4: /spec
      └── Output: docs/phoenix-development-workflow/specs/YYYY-MM-DD-slug-design.md
      └── Gate: user reviews and approves spec

→ Kickoff complete. Spec published. Ready for /dev.
```

---

## Resumability

Stopped mid-kickoff? Just re-run `/kickoff`. The orchestrator reads `docs/kickoff/v<N>/plan.md` and resumes from the last incomplete phase.

---

## What Happens Next

Phase 4 publishes the spec directly to `docs/phoenix-development-workflow/specs/`. No manual copy needed.

Next steps:
1. Run `/project-setup` (if first time) to generate CLAUDE.md
2. Run `/dev` — plan-phase reads the spec and generates execution plans
