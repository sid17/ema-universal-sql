# Plan Template Example

**File naming:** `docs/phoenix-development-workflow/plans/YYYY-MM-DD-NN-phaseN-slug.md`
`NN` links the plan to its parent spec. Date matches the spec in `docs/phoenix-development-workflow/specs/YYYY-MM-DD-NN-feature-design.md`. When multiple specs share the same date, scan existing specs to determine `NN`.

```markdown
# Phase N: [Phase Name]

**Goal:** [One sentence — what this phase delivers]
**Depends on:** [Phase N-1 or "none"]
**Assumes:** [What this phase expects from prior phases — e.g., "Assumes Phase 1 produces the SDK's own session_id, not a generated UUID." If the assumption is wrong, downstream phases break.]
**Verify:** [How to prove this phase is done — specific commands/checks]

---

## File Map

| Action | Path | Responsibility |
|--------|------|---------------|
| Create | `src/components/Graph.tsx` | Main visualization component |
| Modify | `src/App.tsx` | Add router entry |
| Create | `tests/Graph.test.ts` | Component tests |

---

## Phase: Setup
### Task T001: Inspect SDK output (Step 0)
**Files:** none (exploratory)
- [ ] Run `python -c "import sdk; print(sdk.first_message())"` to see actual data shape
- [ ] Compare against spec's data contracts
- [ ] If they differ, flag before proceeding
- [ ] Verify: actual output matches spec

### Task T002: Project scaffold
**Files:** create `package.json`, `tsconfig.json`, `src/index.ts`
**Decision:** Use TypeScript strict mode because the spec requires type safety and the reference implementation uses strict mode.
- [ ] Initialize project with `npm init`
- [ ] Configure TypeScript with strict mode
- [ ] Create entry point with health check endpoint
- [ ] Verify: `npx tsc --noEmit` passes

## Phase: Foundational
### Task T003: Database schema
**Files:** create `src/db/schema.ts`, `src/db/migrate.ts`
**Decision:** Use the SDK's session_id as primary key (not a generated UUID) because the reference implementation captures from SDK and our "Same as" list commits to this.
- [ ] Define User table with id, email, created_at
- [ ] Write migration script
- [ ] Verify: migration runs without errors

## Phase: User Stories
### Task T004 [P] [US1]: User registration
**Files:** create `src/routes/auth.ts`, `tests/auth.test.ts`; modify `src/app.ts`
- [ ] Write POST /register handler with email validation
- [ ] Write test for successful registration
- [ ] Write test for duplicate email rejection
- [ ] Verify: `npm test` passes

---

## Verification Gate

End-to-end flow tests that must pass before the phase is complete:

- [ ] `curl -X POST /register -d '{"email":"test@test.com"}' | jq .id` returns a valid ID
- [ ] `curl /users/1 | jq .email` returns "test@test.com"
- [ ] `npm test` — all tests pass with 0 failures
```

**Key elements:**
- **Assumes** in the header — makes phase dependencies explicit
- **Decision** on choice-involving tasks — records the why, not just the what
- **Step 0** when touching external data — inspect before coding
- **Verification Gate** at the end — flow-level tests, not just per-task checks
- Each task: 2-5 minutes of agent work. Exact file paths. Verification command. No placeholders.
