---
name: debug
description: "Use when tests fail, bugs appear, or build-phase hits repeated failures."
when_to_use: "When investigating a bug or test failure. Called by build-phase when stuck."
domain: dev
category: investigation
inputs: "Bug description or failing test output"
outputs: "Root cause analysis + fix + verification"
handoff: "none"
argument-hint: "[description of the bug]"
---

# Systematic Debugging

4-phase protocol with mandatory root-cause investigation. No fixes without understanding the cause first.

## Context

Debugging is investigation, not guessing. Phase 1 (root cause) is mandatory — you cannot propose fixes without completing it. After 3 failed fix attempts, stop and question the architecture rather than trying a 4th fix.

## Instructions

### Phase 1: Root Cause Investigation (MANDATORY)

Cannot skip this phase. Cannot propose fixes until complete.

1. **Read the error** — full stack trace, exact error message, failing test output
2. **Reproduce** — write a minimal failing test or identify exact steps to trigger
3. **Trace backward** through the call stack:
   - Observe the symptom
   - Find the immediate cause
   - Ask "what called this?"
   - Keep tracing upstream
   - Find the original trigger
4. **Form a single hypothesis** — "The bug is caused by X because Y"

### Phase 2: Pattern Analysis

1. Is this a known failure pattern? (off-by-one, async race, null reference, stale cache)
2. Check related modules for the same issue — bugs cluster
3. Has this been fixed before? Check git log for similar fixes

### Phase 3: Hypothesis Testing

1. Write a minimal test that reproduces the bug (if not done in Phase 1)
2. Test ONE hypothesis at a time — never change multiple things
3. Gather evidence: read specific lines, check types, add targeted logging
4. Confirm or reject the hypothesis before proceeding

### Phase 4: Implementation

1. Fix at the source, not the symptom (fix the original trigger, not where it manifests)
2. Verify the fix:
   - Run the failing test → now passes
   - Run the full test suite → no regressions
3. **Red-green verification** (for critical bugs):
   - Run test with fix → PASS
   - Revert fix → test FAILS (proves the test actually tests the bug)
   - Restore fix → PASS
4. Remove any debug logging added in Phase 3

### Escalation Protocol (3-Attempt Boundary)

| Attempt | Action |
|---------|--------|
| 1 | Fix based on root cause hypothesis |
| 2 | Re-investigate, fix with adjusted hypothesis |
| 3 | Try a fundamentally different approach |
| After 3 | **STOP.** Do not attempt fix #4. |

After 3 failures, report:
- **Tried:** all 3 approaches with specific details
- **Failed:** exact error for each attempt
- **Suspected:** current best hypothesis for root cause
- **Architecture question:** is the approach fundamentally wrong?

### Confusion Protocol (High-Stakes Ambiguity)

When facing decisions with significant architectural implications:
1. STOP — do not guess
2. Name the confusion in one sentence
3. Present 2-3 options with tradeoffs
4. Ask the human to decide

This is for architectural ambiguity, not routine debugging.

## Gates

- [ ] Root cause investigation completed before any fix attempted
- [ ] Fix verified with passing tests
- [ ] No debug logging left in code

## Anti-Patterns

- Complete Phase 1 (root cause) before proposing fixes — fixes without root cause understanding are guesses that often introduce new bugs.
- Test one hypothesis at a time rather than changing multiple things at once — multi-variable changes make it impossible to know which change fixed (or broke) things.
  - BAD: Changing the API handler, the DB query, and the validation in one attempt.
  - GOOD: Change only the DB query, re-run, observe if the error shifts.
- Stop after 3 failed fix attempts and get human approval — repeated failed attempts suggest the mental model is wrong, and a human perspective can reframe the problem.
- Trace to the source rather than fixing at the symptom — symptom fixes leave the root cause active, and the bug resurfaces in a different form.
- Use the confusion protocol for architectural decisions rather than guessing — wrong architectural guesses are expensive to reverse.

## Output Format

Report:
- **Root cause:** 1-2 sentences
- **Fix:** what was changed and why
- **Verification:** test results confirming the fix
- **Related:** other modules that may have the same issue
