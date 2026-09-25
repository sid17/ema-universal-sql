---
name: code-review
description: "Use after completing a PR or before merging a feature branch. Trigger whenever the user mentions code review, PR feedback, wants a second look at changes, says 'review this', 'check my code', or 'is this ready to merge', even if they don't explicitly ask for a 'review'."
when_to_use: "Before merging or deploying. After verify passes. Called by /dev orchestrator."
domain: dev
category: evaluation
inputs: "Git diff + plan file (for intent comparison)"
outputs: "Review report with structured findings"
handoff: "deploy"
argument-hint: "[branch or commit range]"
---

# Code Review

Stage 1 (scope drift) runs before Stage 2 (quality review). Intent alignment catches higher-value issues — building the wrong thing well is worse than building the right thing roughly. Completing Stage 1 first ensures you don't spend review effort on code that shouldn't exist.

Two-stage review: first check you built the right thing, then check you built it well.

**Escape hatch:** If the user says "just approve" or "skip review", confirm once ("Are you sure? This skips scope drift and quality checks."), then respect their decision.

## Context

Single-model code generation has blind spots. Review catches what execution misses: scope drift, logical errors, missed edge cases, over-engineering. Stage 1 (intent alignment) is more valuable than Stage 2 (code quality) — building the wrong thing well is worse than building the right thing roughly.

## Instructions

### Stage 1: Intent Alignment

Compare the diff against the stated intent (plan file, PR description, commit messages).

#### 1a. Scope Drift Detection

For each changed file, classify:
- **In-scope** — directly addresses a plan task
- **Scope creep** — changes not in any plan task
- **Supporting** — infrastructure needed by plan tasks (acceptable)

Output: `Scope Check: CLEAN` or `Scope Check: DRIFT DETECTED — {files}`

#### 1b. Plan Completion Audit

For each plan task, cross-reference against the diff:

| Status | Meaning |
|--------|---------|
| DONE | Task fully implemented in diff |
| PARTIAL | Task started but incomplete |
| NOT DONE | Task not addressed |
| CHANGED | Implementation differs from plan |

For discrepancies, investigate: scope cut? context exhaustion? misunderstood requirement? blocked? forgotten?

### Stage 2: Code Quality Review

Review the diff in priority order:

1. **Security** — injection, auth bypass, exposed secrets, unsafe deserialization
2. **Correctness** — logic errors, off-by-one, null handling, async/await issues, race conditions
3. **Performance** — N+1 queries, unbounded loops, missing indexes, memory leaks
4. **Maintainability** — naming, duplication, complexity, file size, abstraction level
5. **Testing** — coverage gaps, missing edge cases, brittle tests

Do NOT review what linters catch — formatting, import order, unused vars are handled by hooks.

### Finding Format

Every finding must follow this structure:
```
[BLOCKING] Title
**Location:** `src/routes/users.ts:45`
**Evidence:** {what the code does}
**Impact:** {what could go wrong}
**Fix:** {specific suggestion}
```

Severity labels:
- **Blocking** — must fix before merge (security, correctness)
- **Important** — should fix, risk of issues (performance, logic)
- **Nit** — minor improvement (naming, style)
- **Suggestion** — optional enhancement
- **Learning** — knowledge-sharing, not a fix request

### Summary

End with a decision:
- **APPROVE** — no blocking findings, good to merge
- **REQUEST CHANGES** — blocking findings must be addressed
- **NEEDS DISCUSSION** — architectural concerns that need human input

## Gates

- [ ] Stage 1 (intent alignment) completed before Stage 2
- [ ] Every finding has file:line location
- [ ] Every blocking finding has a specific fix suggestion

## Anti-Patterns

- Skip formatting/linting issues — hooks handle those automatically, and reviewing them wastes attention on things already enforced.
- Always include file:line locations in findings — findings without locations force the developer to search for the problem themselves, defeating the purpose of review.
- Show evidence of thorough review rather than saying "looks good" — unsubstantiated approval provides false confidence and misses real issues.
- Address all blocking findings before approving — approving with known blockers means shipping known defects.
- Complete Stage 1 before Stage 2 — intent alignment catches scope drift, which is higher-value than code quality issues on code that may not belong.

## Output Format

**Report:** Scope check result + structured findings with severity + summary decision (APPROVE / REQUEST CHANGES / NEEDS DISCUSSION)
