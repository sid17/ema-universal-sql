---
name: code-quality
description: "Always-on code quality rules. Enforced via CLAUDE.md, not invokable — agent follows these at all times."
type: rule
domain: dev
---

# Code Quality Rules (Always-On)

## LAW 1: File Length Limits

| File Type | Max Lines | Action |
|-----------|-----------|--------|
| Any source file | 500 | Decompose at 400, hard limit at 500 |
| Route handler | 80 | Extract logic to service |

At 400 lines, start planning extraction. At 500, pre-commit hooks will block.

## LAW 2: Import Before Invent

Reuse existing code before writing new. If a function already exists in the codebase, import it. If it's close, extend it. Only write new if nothing suitable exists.

## LAW 3: Single Responsibility

- Each file does one thing
- Each function does one thing
- If a function has 2+ responsibilities, split it

## LAW 4: No Silent Failures

- All caught errors must propagate user-visible state
- No empty catch blocks
- No `catch (e) { /* ignore */ }`
- Log or throw — never swallow

## LAW 5: No Speculative Abstraction

- Only extract a pattern when 3+ uses exist
- Three similar lines of code is better than a premature abstraction
- Don't design for hypothetical future requirements

## LAW 6: Tests Alongside Code

- New code gets tests in the same task, not a follow-up task
- Bug fixes start with a failing test (test-first)
- Test files live next to source files or in a parallel `tests/` tree

## LAW 7: No Config Weakening

- Never disable a linter rule to fix a violation — fix the code
- Never widen a TypeScript type to avoid a type error — fix the types
- Never add `// eslint-disable` without a linked ticket explaining why

