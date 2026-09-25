# Building from Reference Implementations

> When research finds a close reference implementation, follow this guide to avoid the common failure: analyzing a reference thoroughly, then silently diverging in the spec.

---

## 1. Clone and Verify

Clone the reference locally. Don't just read it on GitHub.

```bash
git clone https://github.com/org/reference-repo.git _vendor/reference-repo
```

Run their tests before copying anything:

```bash
cd _vendor/reference-repo
pip install -e . && pytest tests/ -v  # or npm test, etc.
```

This confirms the code works, reveals edge cases, and gives you concrete input/output examples.

## 2. Analyze Concretely

For each feature you plan to build, document how the reference handles it. Focus on:

- **Data flow** — where data originates, transforms, and lands
- **Identity and state** — who generates IDs, what's mutable, where state lives
- **Key abstractions** — classes or patterns that contain complexity
- **Error handling** — what fails and how they recover

Cite exact files and lines. Write observations like "they capture session_id from the first SDK message (sdk-handler.ts:142)" not "they use the SDK's session ID."

## 3. Spec: Same As / Different From

Before writing spec prose, write two lists:

```markdown
## Reference Alignment

### Same as [reference-name]:
- [pattern we're adopting — no justification needed]

### Different from [reference-name]:
- [pattern we're changing]
  **Why:** [justification]
```

**Every "Different" item must have a "Why."** If you can't justify the divergence, move it to "Same as." Unjustified divergences are where bugs hide.

Include **data contracts** with paste-actual-output examples (not descriptions):

```markdown
## Data Contracts
### JSONL first line (verified YYYY-MM-DD):
{"type":"queue-operation","sessionId":"8ff1...","content":"hi"}
Verification: `head -1 path/to/file`
```

Include **user flows** as "what happens when..." scenarios — bugs live in transitions between states, not in the states themselves.

## 4. Plan: Copy, Then Adapt

The most reliable approach: **copy the reference code first, get it working, then adapt.**

Each task should state its source explicitly:

```markdown
- `tts.py` — TTS wrapper
  Copy pattern from: reference's `services/tts.py`
  Keep: function signature, return values, error handling
  Change: swap API call for Edge TTS
  Why: local-first constraint
```

What to copy vs adapt:

| Copy as-is | Adapt | Skip |
|-----------|-------|------|
| Data models, schemas | Service calls (swap providers) | CI/CD, deployment |
| Pipeline orchestration | Model-specific code | Auth, API keys |
| Test structure | File paths, config | Features you don't need |
| Error handling patterns | Dependencies (swap for stack) | |

## 5. Build: Inspect Before Coding

Before writing any parser or data consumer, look at the real data:

```bash
head -5 path/to/actual/data/file
```

Compare against what the spec says. If they differ, update the spec before coding.

Keep exceptions loud during development — silent fallbacks mask divergence from the reference.

Test full flows, not features. The acceptance test is "send → refresh → still there" not "does create return an ID."

Keep `_vendor/` for diffing during debug.

## Checklist

```
RESEARCH:
[ ] Cloned reference locally, ran their tests
[ ] Analyzed with source file:line citations

SPEC:
[ ] "Same as / Different from" with Why for every divergence
[ ] Data contracts with paste-actual-output examples
[ ] User flows as "what happens when..." scenarios

PLAN:
[ ] Each task cites source: "Copy from X, adapt Y"
[ ] Step 0: inspect real data before any phase touching external data
[ ] Verification gates test end-to-end flows

BUILD:
[ ] Copied first, adapted second (not rewritten from scratch)
[ ] Inspected real data before writing parsers
[ ] Tested flows, not features
```
