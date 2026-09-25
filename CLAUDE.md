# ema-assignment

> Take-home assignment for Ema.

---

## The Brief

The assignment brief is the source of truth for scope. Put it in `docs/design/` and treat it as
the requirements — **do not invent scope it does not ask for.** Take-homes are graded partly on
whether you built what was asked.

## Which Workflow to Use

Pick the one that matches what you're doing right now. Only read the docs for that workflow.

| If you're... | Use | Entry point |
|-------------|-----|-------------|
| Turning the brief into architecture decisions and a spec | **Kickoff** | `docs/phoenix-kickoff-workflow/kickoff-workflow.md` |
| Building features, writing tests, reviewing code | **Development** | `docs/phoenix-development-workflow/dev-workflow.md` |

See `phoenix-software-workflow.md` at the repo root for the full command reference.

## Kickoff Workflow

Use when turning the brief into ADRs and a published spec. Read
`docs/phoenix-kickoff-workflow/kickoff-workflow.md` only when doing this work.

**Scoped for a take-home:** the brief already contains the requirements, so `/grilling` is mostly
redundant — skip it or run it only to surface genuine ambiguities worth flagging in the README.
`/architecture-extraction` and `/spec` are the valuable phases. Research (`/github-research`,
`/deep-research`) only if the problem has prior art worth citing.

## Development Workflow

Use when building. Read `docs/phoenix-development-workflow/dev-workflow.md` only when doing this work.

`/kickoff` Phase 4 writes the spec to `docs/phoenix-development-workflow/specs/`, which is exactly
where `/plan-phase` looks. That handoff is what makes the two workflows one pipeline.

## Conventions

- Rules in `.claude/rules/code-quality.md` are always on.
- Hooks in `.claude/settings.json` are the base safety set. A stack overlay gets merged in once
  the stack is chosen.
- Session state: run `/dev handoff` before stopping, `/dev` to resume.
