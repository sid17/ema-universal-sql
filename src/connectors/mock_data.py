"""Deterministic mock datasets for the two connectors.

**Deterministic means literally constant**: no randomness, no ``datetime.now()``,
no generation at import. A reviewer re-running the demo tomorrow must see the
same rows as the screenshots, and the row-count assertions must hold in CI in
six months.

**The persona contract this file exists to satisfy.** Running the canonical
query (open PRs in ``ema/core`` joined to ``In Progress`` issues) under the RLS
rule ``jira.issues.assignee = :user`` must yield:

=========  =====  ===================================================
Persona    Rows   What it proves
=========  =====  ===================================================
(nobody)   8      the unfiltered baseline
``alice``  3      the entitled subset
``bob``    1      RLS **shrinks** the set — non-zero, so it cannot be
                  mistaken for a broken query
``carol``  0      the ``empty`` leg of the trichotomy
=========  =====  ===================================================

``bob`` being 1 rather than 0 is the whole point: a count that drops to zero
looks identical to a query that is simply broken, so the demo would prove
nothing. ``carol``'s zero is deliberate in a different way — she *has* an
``In Progress`` issue (``SUP-31``), but its only pull request is **closed**, so
her emptiness comes from the join rather than from having no data at all.

**The negative rows are load-bearing, not filler.** ``PR#110`` is a *closed* PR
on ``SUP-12`` and ``PR#111`` is an *open* PR on ``SUP-13`` in a *different repo*.
If the ``state`` or ``repo`` predicate silently fails to apply, alice's count
moves 3 → 4 or 5 and the tests catch it. Without rows like these every predicate
in the canonical query is vacuously true, and a filter that does nothing is
indistinguishable from one that works.

**One ``updated`` value is deliberately duplicated.** ``SUP-13`` and ``SUP-14``
share a timestamp, so the result ordering is non-total without its ``key ASC``
tiebreaker. See the comment on that row.

The rows are written as tuples against a column header rather than as literal
dicts purely so each row fits on one readable line — the dicts below are what
the adapters actually serve.
"""

from copy import deepcopy
from typing import Any

JIRA_COLUMNS = ("key", "status", "assignee", "reporter_email", "project", "updated")

#: Every ``reporter_email`` is distinct: it is the column the CLS rule masks, so
#: duplicates would let a masked value be re-identified by matching it against
#: another row that happens to be unmasked.
_JIRA_ROWS = [
    # --- alice: three In Progress, each with exactly one open PR in ema/core ---
    ("SUP-12", "In Progress", "alice", "dana@acme.com", "SUP", "2026-09-27T14:05:00Z"),
    ("SUP-13", "In Progress", "alice", "evan@acme.com", "SUP", "2026-09-27T11:40:00Z"),
    # SUP-14 shares SUP-13's `updated` DELIBERATELY. The result ordering is
    # `updated DESC, key ASC`, and the tiebreaker is not cosmetic: the
    # result cursor is an offset, and an offset over a non-total order skips or
    # duplicates rows between pages. Every other timestamp in this file is
    # distinct, so without this tie the pagination test would page through a
    # totally-ordered set and could never exercise the tiebreaker at all.
    # The tie sits inside alice's three In-Progress issues on purpose: the
    # smallest persona result, so the headline demo exercises it rather than
    # some synthetic case. Changing a timestamp changes no row's MEMBERSHIP in
    # any persona's result, so alice 3 / bob 1 / carol 0 is untouched.
    ("SUP-14", "In Progress", "alice", "fiona@acme.com", "SUP", "2026-09-27T11:40:00Z"),
    # alice also has non-In-Progress work, which must NOT appear.
    ("SUP-15", "Done", "alice", "grace@acme.com", "SUP", "2026-09-25T09:15:00Z"),
    ("SUP-16", "To Do", "alice", "henry@acme.com", "SUP", "2026-09-24T13:00:00Z"),
    # --- bob: exactly one qualifying issue ---
    ("SUP-21", "In Progress", "bob", "iris@acme.com", "SUP", "2026-09-27T08:30:00Z"),
    ("SUP-22", "Done", "bob", "jack@acme.com", "SUP", "2026-09-23T10:45:00Z"),
    ("SUP-23", "To Do", "bob", "kara@acme.com", "SUP", "2026-09-22T15:10:00Z"),
    # --- carol: has In Progress work, but its only PR is closed -> 0 joined rows ---
    ("SUP-31", "In Progress", "carol", "liam@acme.com", "SUP", "2026-09-26T12:00:00Z"),
    ("SUP-32", "Done", "carol", "maya@acme.com", "SUP", "2026-09-21T17:25:00Z"),
    ("SUP-33", "To Do", "carol", "noah@acme.com", "SUP", "2026-09-20T11:05:00Z"),
    # --- other assignees: they make the unfiltered baseline larger than any one
    #     persona's slice, so RLS visibly removes rows that DO exist ---
    ("SUP-41", "In Progress", "dave", "olivia@acme.com", "SUP", "2026-09-27T18:50:00Z"),
    ("SUP-42", "Done", "dave", "peter@acme.com", "SUP", "2026-09-19T14:35:00Z"),
    ("SUP-43", "In Progress", "erin", "quinn@acme.com", "SUP", "2026-09-28T07:15:00Z"),
    ("SUP-44", "To Do", "erin", "rachel@acme.com", "SUP", "2026-09-18T09:00:00Z"),
    ("SUP-45", "Done", "frank", "sam@acme.com", "SUP", "2026-09-17T16:40:00Z"),
    ("SUP-46", "In Progress", "frank", "tara@acme.com", "SUP", "2026-09-26T19:30:00Z"),
    ("SUP-47", "To Do", "grace", "uma@acme.com", "SUP", "2026-09-16T08:20:00Z"),
    ("SUP-48", "Done", "heidi", "victor@acme.com", "SUP", "2026-09-15T12:55:00Z"),
    ("SUP-49", "In Progress", "heidi", "wendy@acme.com", "SUP", "2026-09-25T20:10:00Z"),
]

GITHUB_COLUMNS = (
    "number",
    "title",
    "author",
    "repo",
    "state",
    "issue_key",
    "created_at",
    "updated_at",
)

#: ``issue_key`` is the derived equijoin column — a real repository
#: would parse it out of the branch name or PR title; here it is materialised so
#: the join is a genuine equijoin rather than a fuzzy ``LIKE``.
_GITHUB_ROWS = [
    # --- the eight open / ema-core PRs that join to In Progress issues ---
    (101, "Fix session expiry on refresh", "ana-dev", "ema/core", "open",
     "SUP-12", "2026-09-24T10:00:00Z", "2026-09-27T14:10:00Z"),
    (102, "Retry webhook delivery", "ben-dev", "ema/core", "open",
     "SUP-13", "2026-09-24T12:30:00Z", "2026-09-27T11:45:00Z"),
    (103, "Paginate the audit export", "ana-dev", "ema/core", "open",
     "SUP-14", "2026-09-23T09:20:00Z", "2026-09-26T16:25:00Z"),
    (104, "Cache tenant lookups", "cara-dev", "ema/core", "open",
     "SUP-21", "2026-09-22T14:45:00Z", "2026-09-27T08:35:00Z"),
    (106, "Add connector health probe", "dan-dev", "ema/core", "open",
     "SUP-41", "2026-09-25T11:10:00Z", "2026-09-27T18:55:00Z"),
    (107, "Normalise Jira status names", "eve-dev", "ema/core", "open",
     "SUP-43", "2026-09-26T08:05:00Z", "2026-09-28T07:20:00Z"),
    (108, "Backfill missing issue keys", "finn-dev", "ema/core", "open",
     "SUP-46", "2026-09-21T16:00:00Z", "2026-09-26T19:35:00Z"),
    (109, "Tighten rate-limit headers", "hana-dev", "ema/core", "open",
     "SUP-49", "2026-09-20T13:25:00Z", "2026-09-25T20:15:00Z"),
    # --- the sharp negatives: same issues, wrong state or wrong repo ---
    # If the `state` predicate silently fails to apply, alice's count goes 3 -> 4.
    (110, "Fix session expiry (superseded)", "ana-dev", "ema/core", "closed",
     "SUP-12", "2026-09-18T10:00:00Z", "2026-09-19T10:00:00Z"),
    # If the `repo` predicate silently fails to apply, alice's count goes 3 -> 4.
    (111, "Document webhook retries", "ben-dev", "ema/docs", "open",
     "SUP-13", "2026-09-25T09:00:00Z", "2026-09-26T09:00:00Z"),
    # carol's only PR, closed — the reason her result is empty via the JOIN.
    (105, "Rework tenant offboarding", "cara-dev", "ema/core", "closed",
     "SUP-31", "2026-09-17T15:30:00Z", "2026-09-24T15:30:00Z"),
    # --- open PRs pointing at issues that are Done / To Do ---
    (112, "Polish the settings page", "ivy-dev", "ema/core", "open",
     "SUP-15", "2026-09-19T08:40:00Z", "2026-09-25T09:20:00Z"),
    (113, "Draft the onboarding checklist", "jon-dev", "ema/core", "open",
     "SUP-16", "2026-09-18T17:05:00Z", "2026-09-24T13:05:00Z"),
    (114, "Remove the legacy poller", "kim-dev", "ema/core", "open",
     "SUP-22", "2026-09-16T11:50:00Z", "2026-09-23T10:50:00Z"),
    (115, "Spike: queue backpressure", "leo-dev", "ema/core", "closed",
     "SUP-23", "2026-09-15T14:15:00Z", "2026-09-22T15:15:00Z"),
    (116, "Archive stale exports", "mia-dev", "ema/core", "open",
     "SUP-32", "2026-09-14T10:30:00Z", "2026-09-21T17:30:00Z"),
    (117, "Plan the residency migration", "nic-dev", "ema/core", "open",
     "SUP-33", "2026-09-13T09:45:00Z", "2026-09-20T11:10:00Z"),
    # --- an open PR whose issue_key matches NO issue: un-joinable. The engine
    #     must report these as un-joined rather than dropping them silently or
    #     passing them off as joined (join_status: incomplete).
    (118, "Chore: bump dependencies", "ana-dev", "ema/core", "open",
     "SUP-999", "2026-09-12T12:00:00Z", "2026-09-27T12:00:00Z"),
    # --- PRs in another repo entirely ---
    (119, "Update the API reference", "ben-dev", "ema/docs", "open",
     "SUP-42", "2026-09-11T08:00:00Z", "2026-09-19T14:40:00Z"),
    (120, "Fix broken doc links", "ivy-dev", "ema/docs", "closed",
     "SUP-44", "2026-09-10T16:20:00Z", "2026-09-18T09:05:00Z"),
]

#: ``jira.issues`` as the adapter serves it.
JIRA_ISSUES: list[dict[str, Any]] = [
    dict(zip(JIRA_COLUMNS, row, strict=True)) for row in _JIRA_ROWS
]

#: ``github.pull_requests`` as the adapter serves it.
GITHUB_PULL_REQUESTS: list[dict[str, Any]] = [
    dict(zip(GITHUB_COLUMNS, row, strict=True)) for row in _GITHUB_ROWS
]


def github_rows() -> list[dict[str, Any]]:
    """A deep copy of the PR dataset.

    A copy, not the constant: an adapter that filtered, masked or paginated in
    place would corrupt the dataset for every later request in the process, and
    the corruption would be order-dependent and nearly impossible to reproduce.
    """
    return deepcopy(GITHUB_PULL_REQUESTS)


def jira_rows() -> list[dict[str, Any]]:
    """A deep copy of the issue dataset. See :func:`github_rows`."""
    return deepcopy(JIRA_ISSUES)
