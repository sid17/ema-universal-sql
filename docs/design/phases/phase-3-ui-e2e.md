# Phase 3 — UI Console + Playwright E2E

> **Goal:** a minimal query console that makes the five hard parts *visible in a browser*, and Playwright specs that
> assert each one. **Gate:** all Playwright specs green headless against `make up`; a console screenshot/GIF captured
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

## Behavior
- **Persona control** → `POST /v1/auth/mock-token` for `alice`, `bob`, and a `tenant_globex` user; stores the JWT and sends it as `Authorization: Bearer` on Run.
- **Run** → `POST /v1/query {sql, max_staleness_ms}`; render `rows` in the table with `columns` as headers; a column whose `ColumnMeta.masked=true` renders its cells as `••••`.
- **Metadata panel** renders `freshness_ms`, `join_status`, `partial`, `sources[].served`, `rate_limit_status`, `warnings`, `trace_id` verbatim from the envelope.
- **Banners:** HTTP 429 → a red rate-limit banner showing the `Retry-After` and the async suggestion; a `STALE_DATA` warning → an amber staleness chip; a top-level error → an error banner. Never a hanging spinner — every terminal state resolves to table-or-banner.
- **Pagination:** a **Next** button (disabled when `next_cursor` is null) re-issues the query with `cursor` set to the last `next_cursor`, replacing the table with the next window.
- Add stable `data-testid` attributes on: persona select, staleness input, sql editor, run button, results table, each metadata field, and the banner — so Playwright selectors are robust.

## Playwright specs (`tests/e2e/`) — one behavior each (gate)
| Spec | Steps | Assert |
|---|---|---|
| `rows_render` | persona=alice, Run canonical | table has ≥1 row; headers = title/author/key/status; `trace_id` shown |
| `rls_shrinks_rows` | run as alice, note count; switch to bob, Run | bob's row count `<` alice's (RLS visible) |
| `cls_mask_visible` | persona=alice, Run a query projecting `reporter_email` | its cells render `••••`, never a raw email string |
| `freshness_knob` | staleness=60000 Run (→ `served:cache` on jira); staleness=0 Run | panel `served` flips `cache`→`live`; `stats.connector_ms` populated on live |
| `rate_limit_banner` | click Run rapidly to drain the GitHub bucket | a 429 banner with a retry hint (naming the async option) appears (no infinite spinner) |
| `pagination` | Run a query whose result exceeds the page size; click **Next** | a second, non-overlapping page loads; **Next** disables when `next_cursor` is null |

- Playwright config: `baseURL` = the app; run headless in CI; a `webServer`/`make up` hook brings the stack up first; call the test-only **`POST /v1/test/reset`** (built in Phase 0, `TEST_MODE=1`) in a `beforeEach` to flush Redis buckets + re-seed, so `rate_limit_banner` is deterministic and doesn't poison the other specs.

## Acceptance (gate)
- `make e2e` → all five specs green headless.
- `docs/console.png` captured (a Playwright `page.screenshot` in a spec is fine).

## Done when
The console demonstrates RLS (row count), CLS (masked cells), freshness (served flip), and rate-limit (429 banner) live in a browser, and Playwright asserts all four plus basic rendering. This is the demo a reviewer clicks through.
