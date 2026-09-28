"""Locked inputs and the deterministic mock datasets for the AST tests.

Split out of ``ast_techniques.py`` to keep both files under LAW 1's 400-line
decompose threshold. Every value here is a provenance rail (HLD §4 and §9) — the
canonical query is pinned verbatim to design-doc §6.1, and the datasets are
shaped so RLS yields alice 3 / bob 1 / carol 0.
"""

CANONICAL_SQL = """
SELECT pr.title, pr.author, issue.key, issue.status
FROM   github.pull_requests pr
JOIN   jira.issues issue ON pr.issue_key = issue.key
WHERE  pr.repo = 'ema/core' AND pr.state = 'open' AND issue.status = 'In Progress'
ORDER BY issue.updated DESC
LIMIT 50
"""

# The canonical query projects four columns and none is reporter_email, so it
# cannot demonstrate CLS. This is the console's second preset (HLD §4 note).
CLS_DEMO_SQL = """
SELECT pr.title, pr.author, issue.key, issue.status, issue.reporter_email
FROM   github.pull_requests pr
JOIN   jira.issues issue ON pr.issue_key = issue.key
WHERE  pr.repo = 'ema/core' AND pr.state = 'open' AND issue.status = 'In Progress'
ORDER BY issue.updated DESC
LIMIT 50
"""

SCHEMA = {
    "github": {
        "pull_requests": {
            "id": "INT",
            "repo": "VARCHAR",
            "state": "VARCHAR",
            "title": "VARCHAR",
            "author": "VARCHAR",
            "issue_key": "VARCHAR",
        }
    },
    "jira": {
        "issues": {
            "key": "VARCHAR",
            "status": "VARCHAR",
            "assignee": "VARCHAR",
            "reporter_email": "VARCHAR",
            "updated": "VARCHAR",
            "summary": "VARCHAR",
        }
    },
}

# (db, name) -> the flat name the source's rows get registered under in DuckDB.
REGISTERED = {
    ("github", "pull_requests"): "github_pull_requests",
    ("jira", "issues"): "jira_issues",
}

# --------------------------------------------------------------------------
# Mock datasets (hardcoded — Phase 1 replaces these with the real adapters)
# Shaped for HLD §6: alice 3 rows, bob 1, carol 0.
# --------------------------------------------------------------------------

GITHUB_PULL_REQUESTS = [
    {
        "id": 1,
        "repo": "ema/core",
        "state": "open",
        "title": "Fix retry backoff",
        "author": "alice",
        "issue_key": "SUP-11",
    },
    {
        "id": 2,
        "repo": "ema/core",
        "state": "open",
        "title": "Add cursor pagination",
        "author": "bob",
        "issue_key": "SUP-12",
    },
    {
        "id": 3,
        "repo": "ema/core",
        "state": "open",
        "title": "Harden token bucket",
        "author": "alice",
        "issue_key": "SUP-13",
    },
    {
        "id": 4,
        "repo": "ema/core",
        "state": "open",
        "title": "Mask reporter email",
        "author": "carol",
        "issue_key": "SUP-14",
    },
    {
        "id": 5,
        "repo": "ema/core",
        "state": "open",
        "title": "Bump duckdb",
        "author": "alice",
        "issue_key": "SUP-16",
    },
    {
        "id": 6,
        "repo": "ema/core",
        "state": "open",
        "title": "Refactor planner",
        "author": "bob",
        "issue_key": "SUP-17",
    },
    {
        "id": 7,
        "repo": "ema/core",
        "state": "closed",
        "title": "Old cleanup",
        "author": "alice",
        "issue_key": "SUP-18",
    },
    {
        "id": 8,
        "repo": "ema/platform",
        "state": "open",
        "title": "Platform tweak",
        "author": "alice",
        "issue_key": "SUP-19",
    },
    {
        "id": 9,
        "repo": "ema/core",
        "state": "open",
        "title": "Orphan PR",
        "author": "alice",
        "issue_key": "SUP-99",
    },
]

JIRA_ISSUES = [
    {
        "key": "SUP-11",
        "status": "In Progress",
        "assignee": "alice",
        "reporter_email": "rep1@ema.co",
        "updated": "2026-09-27T10:00:00Z",
        "summary": "Retry storm",
    },
    {
        "key": "SUP-12",
        "status": "In Progress",
        "assignee": "alice",
        "reporter_email": "rep2@ema.co",
        "updated": "2026-09-26T10:00:00Z",
        "summary": "Paging",
    },
    {
        "key": "SUP-13",
        "status": "In Progress",
        "assignee": "alice",
        "reporter_email": "rep3@ema.co",
        "updated": "2026-09-25T10:00:00Z",
        "summary": "Throttling",
    },
    {
        "key": "SUP-14",
        "status": "In Progress",
        "assignee": "bob",
        "reporter_email": "rep4@ema.co",
        "updated": "2026-09-24T10:00:00Z",
        "summary": "PII leak",
    },
    {
        "key": "SUP-16",
        "status": "Done",
        "assignee": "carol",
        "reporter_email": "rep5@ema.co",
        "updated": "2026-09-23T10:00:00Z",
        "summary": "Upgrade",
    },
    {
        "key": "SUP-17",
        "status": "Done",
        "assignee": "alice",
        "reporter_email": "rep6@ema.co",
        "updated": "2026-09-22T10:00:00Z",
        "summary": "Planner",
    },
    {
        "key": "SUP-18",
        "status": "In Progress",
        "assignee": "alice",
        "reporter_email": "rep7@ema.co",
        "updated": "2026-09-21T10:00:00Z",
        "summary": "Cleanup",
    },
    {
        "key": "SUP-19",
        "status": "In Progress",
        "assignee": "alice",
        "reporter_email": "rep8@ema.co",
        "updated": "2026-09-20T10:00:00Z",
        "summary": "Platform",
    },
    {
        "key": "SUP-20",
        "status": "In Progress",
        "assignee": "alice",
        "reporter_email": "rep9@ema.co",
        "updated": "2026-09-19T10:00:00Z",
        "summary": "Orphan issue",
    },
]

DATASETS = {"github_pull_requests": GITHUB_PULL_REQUESTS, "jira_issues": JIRA_ISSUES}
