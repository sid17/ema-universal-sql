# Phase 3 — UI Console + Playwright E2E

> **Goal:** a minimal query console that makes the five hard parts *visible in a browser*, and Playwright specs that
> assert each one. **SHOULD-tier** (the brief says *keep UI minimal*, line 62) — valuable because it makes four of
> the five hard parts visible in a browser, but run it **after Phase 4** if time is short, since Phase 4 holds
> MUST-tier deliverables. `make demo` (Phase 2) already covers the "show me it working" need without a UI.
> **Gate:** all Playwright specs green headless against `make up`; a console screenshot/GIF captured
> as a submission artifact. Depends on Phase 2 (the real envelope).

## Deliverables
1. `ui/` — a single-page console (plain React via CDN + Tailwind, or a tiny Vite app; keep it dependency-light so it serves from the FastAPI `app` container or a static mount).
2. `tests/e2e/` — Playwright specs (TypeScript or Python — match the repo; Python `pytest-playwright` keeps one toolchain).
3. A captured screenshot/GIF of the console → `docs/console.png` (submission artifact).

## UI layout (one page, no routing)
```
┌───────────────────────────────────────────────────────────────────────┐
│  Universal SQL — Query Console                                          │
│  Persona: [ alice ▼ ]  (mints JWT via /v1/auth/mock-token)              │
│  max_staleness_ms: [ 60000 ]                                            │
│  Query: [ canonical ▼ | CLS demo ]   ← preset picker (see note below)   │
│  ┌───────────────────────────────────────────────────────────────┐    │
│  │ SELECT pr.title, pr.author, issue.key, issue.status            │    │
│  │ FROM github.pull_requests pr JOIN jira.issues issue ...        │    │  ← editable, pre-filled canonical
│  └───────────────────────────────────────────────────────────────┘    │
│  [ Run ]                                                                │
│  ── Banner area: 429 / STALE_DATA / errors ──────────────────────────  │
│  ┌── Results table ─────────────────┐ ┌── Metadata panel ───────────┐  │
│  │ title │ author │ key │ status    │ │ freshness_ms: 8200          │  │
│  │ ...   │ alice  │ SUP-12 │ ...     │ │ join_status: complete       │  │
│  │ reporter_email: ••••  (masked)   │ │ partial: false              │  │
│  └──────────────────────────────────┘ │ sources: github(live)       │  │
│                                        │          jira(cache)        │  │
│                                        │ rate_limit_status: {...}    │  │
│                                        │ trace_id: abc123            │  │
│                                        └─────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

> **Why two presets.** The canonical query (HLD §4) projects four columns and **none of them is
> `reporter_email`** — so it cannot demonstrate CLS at all. The console therefore ships two presets: **canonical**
> (the headline query, proves RLS + freshness + rate limits + join degradation) and **CLS demo** (the same query
> plus `issue.reporter_email`, proving the mask). The canonical query itself is unchanged — HLD §9 pins it, and
> changing it would desync this repo from design-doc §6.1.

## Behavior
- **Persona control** → `POST /v1/auth/mock-token` for `alice` (3 rows), `bob` (1 row — the RLS contrast),
  `carol` (0 rows — the empty case), and a `tenant_globex` user; stores the JWT and sends it as
  `Authorization: Bearer` on Run.
- **Run** → `POST /v1/query {sql, max_staleness_ms}`; render `rows` in the table with `columns` as headers.
  **A column with `ColumnMeta.masked = true` renders `••••` regardless of the mask kind, and the UI never
  displays the masked value itself.** With the seeded `mask: hash` the cell *value* is an MD5 digest, not
  `••••` — so a console that printed values would show a hash and the spec below would fail. Render off the
  `masked` flag, not off the data.
- **Metadata panel** renders `freshness_ms`, `join_status`, `partial`, `sources[].served`, `rate_limit_status`, `warnings`, `trace_id` verbatim from the envelope.
- **Banners:** HTTP 429 → a red rate-limit banner showing the `Retry-After` and the async suggestion; a `STALE_DATA` warning → an amber staleness chip; a top-level error → an error banner. Never a hanging spinner — every terminal state resolves to table-or-banner.
- **Pagination:** a **Next** button (disabled when `next_cursor` is null) re-issues the query with `cursor` set to the last `next_cursor`, replacing the table with the next window.
- Add stable `data-testid` attributes on: persona select, **query-preset select**, staleness input, sql editor, run button, results table, each metadata field, and the banner — so Playwright selectors are robust.

## Playwright specs (`tests/e2e/`) — one behavior each (gate)
| Spec | Steps | Assert |
|---|---|---|
| `rows_render` | persona=alice, Run canonical | table has ≥1 row; headers = title/author/key/status; `trace_id` shown |
| `rls_shrinks_rows` | run as alice, note the count; switch to bob, Run | bob's count is **lower but non-zero** (3 → 1) — assert both, since asserting only "fewer" would pass for a broken query returning nothing |
| `cls_mask_visible` | persona=alice, pick the **CLS demo** preset (the canonical query does not project `reporter_email`) | cells render `••••`; assert additionally that no `@` appears anywhere in that column, so a regression that leaked the raw email fails even if `••••` were also present |
| `freshness_knob` | the `beforeEach` reset flushes Redis, so the cache starts **cold** — run **live first**: staleness=0 Run, then staleness=60000 Run | `served` flips `live` → `cache` (in that order); `stats.connector_ms` is populated on the live run and absent for the cached side |
| `rate_limit_banner` | click Run rapidly to drain the GitHub bucket | a 429 banner with a retry hint (naming the async option) appears (no infinite spinner) |
| `pagination` | Run a query whose result exceeds the page size; click **Next** | a second, non-overlapping page loads; **Next** disables when `next_cursor` is null |

- Playwright config: `baseURL` = the app; run headless in CI; a `webServer`/`make up` hook brings the stack up first; call the test-only **`POST /v1/test/reset`** (built in Phase 0, `TEST_MODE=1`) in a `beforeEach` to flush Redis buckets + re-seed, so `rate_limit_banner` is deterministic and doesn't poison the other specs.

## Acceptance (gate)
- `make e2e` → all five specs green headless.
- `docs/console.png` captured (a Playwright `page.screenshot` in a spec is fine).

## Done when
The console demonstrates RLS (row count), CLS (masked cells), freshness (served flip), and rate-limit (429 banner) live in a browser, and Playwright asserts all four plus basic rendering. This is the demo a reviewer clicks through.
