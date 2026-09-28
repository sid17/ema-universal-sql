"""Per-tenant synthetic datasets, for load testing only.

**Why this is not `mock_data.py`.** Those twenty rows are fixtures: committed
assertions depend on their exact contents — the SUP-13/SUP-14 tie-break, the CLS
masking demo, the canonical-query integration test. Growing them to load-test
size would rewrite those tests for a reason that has nothing to do with what
they assert. So the load path gets its own generator and the fixtures are left
alone.

**Three properties the load test needs and the fixtures cannot give it.**

1. **Per-tenant rows.** Every tenant currently sees the same twenty rows, which
   means a cross-tenant leak under load would be *invisible*. Here each tenant's
   rows carry its own ``tenant_id`` in ``title`` — a projected column — so k6
   can assert on every response that no foreign stamp appears. That turns tenant
   isolation into something checked under concurrency, which is the only place
   it can realistically break.

2. **A key space.** The cache key is built from the pushed-down predicates, so
   *the WHERE-clause literals are the key space*. Rows exist across
   ``keyspace`` repositories (GitHub) and projects (Jira), letting a load
   generator draw a key per request and produce a genuine, measurable hit ratio
   rather than a configured one.

3. **Realistic cardinality.** A real ``repo=X AND state=open`` returns tens to
   low hundreds of open PRs, not twenty. The join cost is only data-dependent if
   the data is.

Deterministic: the same ``(tenant_id, rows, keyspace)`` always produces the same
dataset, so a run is reproducible and no test can flake on generated content.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

#: The user every load token is minted for. Rows are assigned to it often enough
#: that the RLS predicate (`assignee = :user`) leaves a non-trivial result —
#: a load test whose every query legitimately returns zero rows would measure
#: the parser and nothing else.
LOAD_USER = "loadbot"

#: Repository / project names are derived from the key index, so a caller
#: selects a cache key by choosing an integer.
REPO_TEMPLATE = "ema/svc-{key}"
PROJECT_TEMPLATE = "SVC{key}"


def repo_for(key: int) -> str:
    """The GitHub ``repo`` literal for key index ``key``."""
    return REPO_TEMPLATE.format(key=key)


def project_for(key: int) -> str:
    """The Jira ``project`` literal for key index ``key``."""
    return PROJECT_TEMPLATE.format(key=key)


def synthetic_rows(
    tenant_id: str, connector_type: str, rows: int, keyspace: int
) -> list[dict[str, Any]]:
    """The full unfiltered dataset one tenant's connector would return.

    Returns per-row copies rather than the cached originals: every value is a
    scalar, so a shallow copy is sufficient isolation and costs a fraction of
    the ``deepcopy`` the fixture path uses.
    """
    return [dict(row) for row in _build(tenant_id, connector_type, rows, keyspace)]


@lru_cache(maxsize=64)
def _build(
    tenant_id: str, connector_type: str, rows: int, keyspace: int
) -> tuple[dict[str, Any], ...]:
    """Generate once per (tenant, connector, size) and keep it.

    Cached because a cache *miss* on the connector regenerates this, and at load
    a miss must cost a fetch rather than a fetch plus a dataset build.
    """
    if connector_type == "github":
        built = _github(tenant_id, rows, keyspace)
    elif connector_type == "jira":
        built = _jira(tenant_id, rows, keyspace)
    else:
        # An unknown connector is a wiring bug. Returning [] would look
        # exactly like a tenant with no data and hide it.
        raise ValueError(
            f"no synthetic dataset for connector {connector_type!r}; expected 'github' or 'jira'"
        )
    return tuple(built)


def _issue_key(key: int, index: int) -> str:
    """The join key. Shared by both generators — this is what makes them join."""
    return f"SVC{key}-{index}"


def _github(tenant_id: str, rows: int, keyspace: int) -> list[dict[str, Any]]:
    return [
        {
            "number": key * rows + index,
            # The leak stamp. `title` is in the canonical query's projection, so
            # it reaches the response and k6 can check it on every row.
            "title": f"[{tenant_id}] pr-{key}-{index}",
            "author": f"u{index % 7}",
            "repo": repo_for(key),
            # Two-thirds open: the query filters on `state='open'`, and a filter
            # that matches everything is not a filter.
            "state": "closed" if index % 3 == 0 else "open",
            "issue_key": _issue_key(key, index),
            "created_at": f"2026-08-{index % 28 + 1:02d}T00:00:00Z",
            "updated_at": f"2026-09-{index % 28 + 1:02d}T00:00:00Z",
        }
        for key in range(keyspace)
        for index in range(rows)
    ]


def _jira(tenant_id: str, rows: int, keyspace: int) -> list[dict[str, Any]]:
    return [
        {
            "key": _issue_key(key, index),
            # Alternating, and deliberately NOT correlated with `state` above:
            # if both filters selected the same rows the join would be a
            # straight-through pass and the predicate work would be free.
            "status": "In Progress" if index % 2 else "Done",
            # The RLS subject. Two-thirds belong to the load user, so the
            # entitled result is large enough to page but still a real subset.
            "assignee": f"u{index % 7}" if index % 3 == 0 else LOAD_USER,
            # CLS-masked, so this never reaches a response in the clear — which
            # is exactly why the leak stamp lives in `title` and not here.
            "reporter_email": f"r{index}@{tenant_id}.example.com",
            "project": project_for(key),
            "updated": f"2026-09-{index % 28 + 1:02d}T00:00:00Z",
        }
        for key in range(keyspace)
        for index in range(rows)
    ]
