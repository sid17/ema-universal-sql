# ema-assignment

> Take-home for Ema: a universal SQL query layer over enterprise SaaS apps. One cross-app query
> (GitHub PRs joined to Jira issues) end to end, with entitlement, rate limits and freshness.

**The prototype is built and green.** 689 unit + 113 integration tests, ruff clean. Start from
`README.md` — it is the reviewer-facing entry point and should stay under ~200 lines.

## Run and verify

```bash
make up && make seed     # stack + control plane
make test                # unit suite, no Docker needed
make test-integration    # needs the stack
make artifacts           # regenerate every committed artifact
```

## Where things live

| | |
|---|---|
| `src/gateway/` | auth, routes, the four entitlement gates (identity → tenant → scope → RLS/CLS) |
| `src/sqlparse/` | sqlglot parse + the supported-subset whitelist |
| `src/entitlement/` | RLS predicate and CLS mask compiled **into** the AST |
| `src/planner/` | pushdown split — what the source filters vs what we re-apply |
| `src/execution/` | parallel fetch, DuckDB join, envelope assembly |
| `src/connectors/` | the adapter contract: build a call, answer it, parse it back |
| `src/governance/` | token bucket, freshness cache, per-tenant secrets, audit |
| `config/` | policies, grants, budgets, one file per connector — all YAML |
| `docs/artifacts/` | generated evidence — never hand-edit, always regenerate |

## Invariants — do not break these

1. **Entitlement is compiled into the plan and pushed down, never post-filtered.** The moment rows
   we are not entitled to are fetched and dropped in Python, the central safety claim is false.
2. **The SQL executed is the whole entitled tree**, including predicates a source already applied.
   That is what makes pushdown an optimization rather than a correctness dependency.
3. **Cache before token, never token before cache.** A cache hit makes no downstream call, so
   charging it a token would be wrong.
4. **`register()` is cursor-local.** The per-tenant DuckDB pool's safety rests on it.
5. **A partial result never returns a cursor.** Paging from an incomplete page skips rows silently.

## Conventions

- Rules in `.claude/rules/code-quality.md` are always on.
- **Comments must be self-contained.** No `ADR-NNN`, `design-doc §X`, `HLD §Y`, `brief line N`, or
  phase numbers in `src/`, `scripts/`, `config/` or the `Makefile` — a reader has only this
  repository. Keep the argument, drop the citation.
- Artifacts under `docs/artifacts/` are generated. Change the code or the script, then regenerate.
- Numbers quoted in docs must have a committed artifact behind them.

## The build record

This was built with an AI-assisted workflow, and that record is kept deliberately —
`docs/kickoff/v*/` holds the architecture decisions and prior-art research per round,
`docs/phoenix-development-workflow/{specs,plans}/` the per-phase design specs and task plans.
`phoenix-software-workflow.md` is the command reference; `docs/phoenix-development-workflow/dev-workflow.md`
and `docs/phoenix-kickoff-workflow/kickoff-workflow.md` are the two workflow entry points.

The design document and six-month execution plan are the Google Doc, not in this repo.
