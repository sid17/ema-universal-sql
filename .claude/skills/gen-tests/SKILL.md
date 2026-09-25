---
name: gen-tests
description: "Use when new code needs tests, test coverage is missing, or after modifying existing code. Trigger when the user says 'write tests', 'add test coverage', 'generate tests', or mentions untested code, even for a single file or endpoint."
argument-hint: "[backend|mobile] [optional: specific file or endpoint]"
allowed-tools: Read Grep Glob Write
---

# Generate Tests

Auto-generate tests for new or modified code.

## Step 1: Determine scope

If `$ARGUMENTS` specifies a file, generate tests for that file.
If `$ARGUMENTS` is "backend" or "mobile", scan for untested code.
If no arguments, check git diff for recently changed files and generate tests for those.

## Step 2: Backend tests (pytest)

Read architecture docs if available (e.g., `docs/architecture/backend.md`). Look for existing test patterns in the project — scan for `conftest.py`, existing test files, and fixture conventions. Adapt to whatever structure the project uses rather than assuming specific paths.

For each endpoint or module in scope:
1. Read the source code
2. Read the model/schema it uses (if any)
3. Generate tests following existing project conventions (or `tests/test_{domain}.py` if no conventions found):
   - Happy path (200 response, correct shape)
   - Validation errors (missing fields, wrong types)
   - Edge cases (empty results, pagination boundaries)
   - Error cases (404 for missing resources)

**Pattern** — follow existing tests in the project if found, otherwise use this default:
```python
class TestEndpointName:
    def test_happy_path(self, client):
        resp = client.get("/endpoint")
        assert resp.status_code == 200
        data = resp.json()
        # assert shape

    def test_not_found(self, client):
        resp = client.get("/endpoint/nonexistent")
        assert resp.status_code == 404
```

Use shared fixtures from existing `conftest.py` if present, otherwise create them.
Clean up test data after each test.

## Step 3: Mobile tests (Jest)

Read `docs/architecture/mobile.md` for structure.

For hooks and utils:
1. Read the source file
2. Generate tests following project conventions (e.g., `__tests__/{name}.test.ts` colocated or `tests/` at root):
   - Test each exported function
   - Test edge cases (empty input, null values)
   - Mock external dependencies (database, fetch, navigation)

Follow existing mock patterns in the project if found.

## Step 4: Validate

- Backend: `poetry run pytest tests/ -v --tb=short`
- Mobile: `cd mobile && yarn test`

Report: number of tests generated, all passing.
