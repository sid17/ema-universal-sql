# Handoff

> Last updated: 2026-09-28 (Session 2)

## Project

Take-home for Ema: **Universal SQL across enterprise apps** — a federated query layer that runs one cross-app
SQL query (GitHub PRs ⋈ Jira issues) end-to-end with query-time entitlement (RLS/CLS compiled into the AST),
per-tenant rate limiting, staleness-controlled caching, and honest partial-result degradation.

Session 1 produced the design set. **Session 2 built, reviewed and committed Phase 0.** The next agent starts
Phase 1.

## Plan Status

| Phase | Tier | Spec | Plan | Status | Commits |
|---|---|---|---|---|---|
| 0 — scaffold + contracts | MUST | `specs/2026-09-28-phase0-scaffold.md` | `plans/2026-09-28-phase0-scaffold.md` (98/98) | ✅ **Done** | 11, `e5c2dd0`…`158102b` |
| 1 — connectors + governance | MUST | `docs/design/phases/phase-1-connectors.md` | — | Not started | — |
| 2 — SQL pipeline | MUST | `docs/design/phases/phase-2-sql-pipeline.md` | — | Not started | — |
| 4 — observability + load + README | MUST | `docs/design/phases/phase-4-observability.md` | — | Not started | — |
| 3 — UI console + Playwright | SHOULD | `docs/design/phases/phase-3-ui-e2e.md` | — | Not started | — |

Build in **tracker order (0 → 1 → 2 → 4 → 3)**, not numeric order — Phase 4 holds four MUST-tier deliverables
while Phase 3 is SHOULD-tier, and both depend only on Phase 2.

**Phase 0 gate: green.** 115 unit + 17 integration tests, `ruff check` clean with no ignore list, cold start
**46.7s** including a full image build (gate: 60s), largest file 307 lines.

## What Was Done This Session

Phase 0 ran the full five-step loop from `03-BUILD-PROCESS.md`: spike → research → ADRs → spec → plan (human
gate) → build → review → commit.

| Commit | What |
|---|---|
| `e5c2dd0` | **Corrected research Card 1 from the AST spike.** See "What Didn't Work" — this is the most valuable thing the session produced. |
| `e2e23ee` | Kickoff trail: `research-repos.md`, `architecture.md` (18 ADRs), the spec, and the 20-task plan. |
| `d0fff0d` | `pyproject.toml` (all Phase 0–4 deps up front), `Dockerfile`, three-service `docker-compose.yml`, `Makefile`, `.env.example`. |
| `d040fe3` | Contract models: `QueryEnvelope` (HLD §4 verbatim), the six-code `ErrorCode`, `UserContext`, `QueryRequest`, `config.py`. |
| `d29ae2f` | Control plane: 7-table schema, idempotent migration runner, TTL-cached repository, `002_seed_tenants.sql`. |
| `01bc4e7` | Gateway: mock JWT auth, tenant gate, scope gate, six routes. |
| `2b3fc98` | Observability: `@stage_span`, Prometheus registry, JSONL span export, structured access log. |
| `092981d` | FastAPI factory + the integration gate against the running stack. |
| `1e0ab19` | Promoted the AST spike to `tests/unit/test_ast_spike.py`. |
| `254ea3f` | README: quickstart + the mock-IdP caveat. |
| `158102b` | `.env.example` signing-key fix. |

**Independent security review found no blocking issues.** Verified by actually forging tokens against the
repo's own modules: `alg:none`, algorithm confusion (HS512 with the *correct* secret), RS256 with an attacker
keypair, missing/wrong `exp`/`aud`/`iss` — all rejected. No SQL injection (every query funnels through one
parameterised chokepoint). No fail-open path through `get_current_user`; a missing repository fails *closed*.
The post-filter ban holds structurally.

## What Didn't Work

Six things were found wrong and fixed. The pattern connecting most of them: **a claim that was documented but
not true, where nothing failed loudly.**

- **The AST spike's first run silently disabled all pushdown.** Research Card 1 said to split predicates with
  `where.this.flatten()`. Once RLS is injected that is wrong — `tree.where(pred, append=True)` routes through
  `exp.and_`, which wraps the existing WHERE in an `exp.Paren`; `flatten()` prunes at the paren and returns the
  whole nested AND as one leaf, so **zero** predicates were pushable to GitHub. The query still returned
  correct rows. Every result-based test would have stayed green while the design doc's central claim quietly
  became false. Must recurse through `.unnest()`. **This is why the spike existed, and it justified its cost.**
- **`UserContext` claimed an immutability it did not have — twice.** First version used `roles: list`, so
  `context.roles.append("admin")` succeeded inside the object whose roles select the RLS policies. The first
  *fix* coerced only inside `AuthContextExtractor` — one construction site, leaving every test fixture and
  future caller free to rebuild the seam. An independent review caught that. The guarantee now lives on the
  type via `__post_init__`.
- **The tenant gate made Phase 0 unable to pass its own gate.** The phase file says all three of: build the
  tenant-status gate, seed nothing, and return 200 on a valid token. With `tenants` empty every authenticated
  request was correctly refused `403`. Resolved by seeding tenants in Phase 0 — the alternative (letting
  unknown tenants through) fails open.
- **The 403 bodies were a tenant-enumeration oracle.** `Unknown tenant: X` vs `Tenant X is offboarding` are
  distinguishable, and the mock IdP lets anyone name any tenant. Now one byte-identical body for both.
- **The control-plane cache was unbounded and attacker-growable.** Keyed by `tenant_id` from a token anyone can
  mint; misses cached too. Measured 50,000 entries from 50,000 ids. Now bounded + LRU.
- **Two tests asserted nothing.** `test_migrations_created_the_control_plane` re-asserted `/healthz == 200`
  (passes against an empty database); `test_test_reset` accepted "404 or 200" and skipped on 200 (passes if the
  guard is deleted entirely). Both rewritten.

Minor time sinks worth not repeating: FastAPI 0.141 wraps `include_router` in an `_IncludedRouter` so routes no
longer appear flat in `app.routes` (introspection misleads — use a TestClient); `ConsoleSpanExporter` binds
`sys.stdout` at import, so `capsys` cannot capture it.

## What's Next

- **Immediate next action: Phase 1** — `docs/design/phases/phase-1-connectors.md`. Two mock adapters with
  capability models, `TokenBucketRateLimiter` (Redis Lua, with burst), `FreshnessCacheManager`,
  `SecretsManagerClient` (Fernet), and the YAML seed.
- Run the same loop: lightweight `/github-research` → `/architecture-extraction` → `/spec` → `/plan-phase`
  (**hard gate: human reviews the plan**) → `/build-phase`.
- Plan file: none yet for Phase 1. Last completed: **Phase 0, all 20 tasks**.

**Scope: DECIDED — build all five phases.** Confirmed 2026-09-28 after Phase 0 shipped, with its real cost
known rather than as an up-front guess. Neither effort lever is pulled: the SHOULD-tier console and Playwright
specs are in. Build order is unchanged (**0 → 1 → 2 → 4 → 3**) — Phase 3 being committed is not a reason to
pull it ahead of Phase 4, which holds four MUST-tier deliverables. The COULD list stays opportunistic; prose is
an acceptable answer for each of those items. Recorded in `03-BUILD-PROCESS.md` (*Effort reality check*),
`01-EXECUTION-PLAN.md` (scope ledger) and `02-DEFINITION-OF-DONE.md` §3.

**One open question for the user, carried from Session 1:**

1. **The submitted Google Doc is still missing design-doc §6 and §8** — including §6.4, where the access grant
   to `souvik-sen@` / `careers@` is stated (submission checklist lines 50/165). Both sections exist in
   `docs/design/design-doc.md`. This is a paste, and it is still the highest-value fix available.

## Key Decisions

- **Scope: all five phases** (0 → 1 → 2 → 4 → 3), decided 2026-09-28 with Phase 0's real cost known. Phase 3
  is committed rather than conditional; the COULD list stays opportunistic. The cut order in
  `02-DEFINITION-OF-DONE.md` §3 is not retired — it is insurance on an estimate (≈18–24h) that is the most
  likely thing to slip.
- **Four new ADRs**, all Accepted, in `docs/kickoff/v1/architecture.md`:
  **015** `/metrics` is one route we own (collectors via `.instrument(app)`, never `.expose(app)`) ·
  **016** no OTLP exporter or Jaeger container — compose stays three services against the 60s cold-start gate ·
  **017** identity carries **both** OAuth `scopes` and SCIM `roles`, with authorization layered L0–L4 ·
  **018** spans export to JSONL off-thread (amends 016).
- **The authorization rule to not break:** *an endpoint check may consult the token and the control plane,
  never a result row.* L0 authN → L1 tenant → L2 scope at the gateway; L3 connector grant and L4 RLS/CLS are
  Phase 2, and L4 is **compiled into the plan**. A test asserts `require_scope` takes no repository.
- **Observability is front-loaded.** The span decorator and Prometheus registry exist now, so Phase 2
  decorates each stage as it writes it and Phase 4 shrinks to k6 + artifacts + README.
- Locked decisions from Session 1 are unchanged and remain closed: the stack, mock-only connectors, policy as
  JSONB predicate AST, the equijoin, RLS `assignee = :user`, CLS mask `reporter_email` (`hash`), the canonical
  query verbatim, every rail in HLD §9, and the three non-negotiables in `02-DEFINITION-OF-DONE.md` §4.

## Watch-outs

These will bite the next session specifically.

- **Phase 1's seed file must be `003_seed.sql`.** `002_seed_tenants.sql` took the `002_` slot (see "What Didn't
  Work"). The execution plan still calls Phase 1's seed `002_seed.sql` — it is wrong.
- **The per-request deadline is STORED BUT NOT ENFORCED.** `routes.py` sets `request.state.deadline_ms` and
  nothing reads it. The spec describes it as "bounding the whole pipeline", so **Phase 2 must actually enforce
  it** — otherwise a slow source hangs the request instead of degrading to `partial`, which is brief line 84.
  Left deliberately: there is no pipeline to bound yet.
- **Phase 2 must normalise `query_text` before writing `audit_logs` rows.** A predicate literal like
  `WHERE reporter_email = 'x@acme.com'` would put in the audit table exactly the PII the CLS rule strips from
  the result. sqlglot makes parameterising it nearly free at that point.
- **Phase 4 must run k6 with `OTEL_EXPORTER=none`** and capture the waterfall from a separate `file`-mode run.
  The variable is plumbed through compose. Exporting during the load run is overhead on the number k6 reports.
- **`src/models/` is frozen.** Both P1 and P2 code against it. Changing the envelope means changing the HLD,
  the phase specs *and* the submitted design doc together.
- **The mock IdP mints any tenant/role/scope unauthenticated**, so the tenant and scope gates are
  *demonstrable, not enforceable*. Documented in the README. What stays adversarially meaningful is that
  entitlement is compiled into the plan — do not let Phase 2 weaken that.
- **`python` is not on PATH — use `.venv/bin/python`** (pinned to 3.11.15 via `uv`).
- **Hooks are live**: files over 500 lines block the commit; `pytest -q tests/unit` must pass to commit (so
  `tests/unit` must stay infra-free); adding `ignore`/`noqa`/`disable` to a config is blocked; `ruff format`
  runs on every `.py` edit.
- **`FederationEngine` must stay split** into `federation.py` + `assemble.py` when Phase 2 builds it, or it
  breaches LAW 1 and LAW 3 and the commit hook blocks it.
- **Two personas, not one, carry the RLS demo:** alice 3 rows, bob **1** (not 0 — a count that collapses to
  zero is indistinguishable from a broken query), carol 0 for the `empty` leg.
- **`tenant_acme`'s GitHub budget is deliberately 5 req/60s** for the 429 demo. k6 must target `tenant_load`.

## Key Files

| File | Why it matters |
|---|---|
| `docs/design/00-PROTOTYPE-HLD.md` §9 | the provenance rails — every value pinned there is depended on by more than one phase |
| `docs/design/02-DEFINITION-OF-DONE.md` | the submission gate, scope tiers, and the three non-negotiables |
| `docs/design/03-BUILD-PROCESS.md` | the per-phase loop and the phase tracker (Phase 0 ticked) |
| `docs/kickoff/v1/architecture.md` | 18 ADRs, all Accepted and closed to re-decision |
| `docs/design/phases/phase-1-connectors.md` | **the next thing to build** |
| `src/models/envelope.py` | the frozen response contract every later phase fills |
| `src/gateway/deps.py` | the authorization layering, and the rule that keeps it honest |
| `src/control_plane/repository.py` | the cached read layer P1 and P2 both go through |
| `tests/unit/test_ast_spike.py` | the promoted spike — Phase 2 rewrites this against the real planner |
