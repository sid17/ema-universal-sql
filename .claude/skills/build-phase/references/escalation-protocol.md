# Escalation Protocol (3-Attempt Boundary)

When a task or fix fails:

- **Attempt 1-2:** Fix and retry using the same general approach
- **Attempt 3:** Try a fundamentally different approach (different algorithm, different library, different architecture)
- **After 3 failures:** STOP. Do not attempt fix #4. Report to the user:
  - What was tried (all 3 approaches, with specific details)
  - What failed (specific errors, not summaries)
  - What's suspected (root cause hypothesis)
  - Whether the architecture should be questioned

The 3-attempt boundary exists because fix #4 is almost always a variant of fixes 1-3. Human judgment is needed to break out of the loop.
