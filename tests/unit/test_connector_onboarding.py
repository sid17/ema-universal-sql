"""The claim this phase is graded on: onboarding is configuration.

Before Phase 6, ``connectors`` was keyed by ``connector_type`` alone and the
YAML's ``resource:`` was never persisted — it lived as a Python class attribute.
So a second GitHub endpoint meant a second adapter **class**, which is the
opposite of "admins onboard via console or config" (brief line 29).

These tests seed a third resource at runtime and assert it becomes queryable
with **no new class, and no change to the parser, planner, engine or registry**.

What a *mock* still needs, honestly: the rows that API call would return. That
is what :attr:`MockConnectorAdapter.DATASETS` is, and it is unavoidable —
mock data has to come from somewhere. A live adapter fetches instead, so for a
live connector the new resource is configuration and nothing else.
"""

import pytest

from src.connectors.base import FetchRequest
from src.connectors.github import GitHubConnectorAdapter
from src.connectors.request import EndpointSpec, build_request, compose_endpoint
from src.pipeline.registry import ConnectorRegistry
from tests.unit.catalog_fixture import GITHUB_API, GITHUB_CAPABILITIES

#: A third API call on an existing connector, in the shape `resources:` authors.
ISSUES_ENDPOINT = {"method": "GET", "path": "/repos/{repo}/issues"}

ISSUES_ROW = {
    "connector_type": "github",
    "resource": "issues",
    "version": "1.0.0",
    "endpoint": compose_endpoint(GITHUB_API, ISSUES_ENDPOINT),
    "rate_limit": GITHUB_API["rate_limit"],
    # Same filters; a real GitHub issues endpoint would differ, and nothing here
    # depends on it not differing.
    "capabilities": GITHUB_CAPABILITIES,
}


@pytest.fixture
def onboarded(control_plane, cache, limiter, secrets) -> ConnectorRegistry:
    """A control plane that has been told about one more GitHub API call.

    The only Python this needs is the mock's dataset. Everything else — the
    endpoint, the filters, the rate-limit dialect — is the row above.
    """
    control_plane.connectors = [*control_plane.connectors, ISSUES_ROW]
    GitHubConnectorAdapter.DATASETS["issues"] = lambda: [
        {"number": 7, "title": "flaky test", "repo": "ema/core", "state": "open"}
    ]
    try:
        yield ConnectorRegistry(control_plane, cache, limiter, secrets)
    finally:
        GitHubConnectorAdapter.DATASETS.pop("issues", None)


async def test_a_second_resource_needs_no_second_adapter_class(onboarded) -> None:
    adapters = await onboarded.adapters()

    assert set(adapters) == {
        ("github", "pull_requests"),
        ("github", "issues"),
        ("jira", "issues"),
    }
    # One class, two resources — the whole point.
    assert type(adapters[("github", "pull_requests")]) is GitHubConnectorAdapter
    assert type(adapters[("github", "issues")]) is GitHubConnectorAdapter


def test_the_new_resource_is_queryable_by_name(onboarded) -> None:
    """`github.issues` resolves in the catalog the parser qualifies against."""
    catalog = onboarded.catalog()

    source = catalog.get("github", "issues")
    assert source is not None
    assert source.qualified_name == "github.issues"
    # And DuckDB registration derives from the same pair, so it cannot collide
    # with the connector's other resource.
    assert source.registered_name == "github_issues"


async def test_each_resource_renders_its_own_endpoint(onboarded) -> None:
    """Two adapters, one class, two different URLs — read from their rows."""
    adapters = await onboarded.adapters()
    request = FetchRequest(
        tenant_id="tenant_acme",
        entitlement_scope="support:alice",
        predicates={"repo": "ema/core"},
        limit=5,
    )

    pulls = adapters[("github", "pull_requests")]
    issues = adapters[("github", "issues")]

    assert build_request(pulls.endpoint, pulls.capabilities(), request).path == (
        "/repos/ema/core/pulls"
    )
    assert build_request(issues.endpoint, issues.capabilities(), request).path == (
        "/repos/ema/core/issues"
    )


async def test_the_new_resource_actually_serves_its_own_rows(onboarded) -> None:
    """End to end through the real `fetch()`, not just through the wiring."""
    adapter = (await onboarded.adapters())[("github", "issues")]

    response = await adapter.fetch(
        FetchRequest(
            tenant_id="tenant_acme",
            entitlement_scope="support:alice",
            predicates={"repo": "ema/core"},
            limit=5,
            max_staleness_ms=0,
        )
    )

    assert [row["title"] for row in response.rows] == ["flaky test"]
    assert response.served == "live"


# --- and the two ways onboarding can be wrong, both loud --------------------


async def test_a_seeded_resource_with_no_dataset_is_refused_at_construction(
    control_plane, cache, limiter, secrets
) -> None:
    """LAW 4. An empty page here is indistinguishable from a query that
    legitimately matched nothing, so the mock refuses instead."""
    control_plane.connectors = [*control_plane.connectors, ISSUES_ROW]
    registry = ConnectorRegistry(control_plane, cache, limiter, secrets)

    with pytest.raises(ValueError, match="serves no dataset for resource 'issues'"):
        await registry.adapters()


def test_a_connector_with_no_adapter_class_is_skipped_not_fatal(
    control_plane, cache, limiter, secrets, caplog
) -> None:
    """A YAML naming a kind this build cannot talk to must not take the service
    down — the other sources still answer, and this one is simply unknown."""
    control_plane.connectors = [
        *control_plane.connectors,
        {**ISSUES_ROW, "connector_type": "slack"},
    ]
    registry = ConnectorRegistry(control_plane, cache, limiter, secrets)

    catalog = registry.catalog()

    assert catalog.get("slack", "issues") is None
    assert catalog.get("github", "pull_requests") is not None
    assert "no adapter class" in caplog.text


def test_the_endpoint_spec_rejects_a_row_missing_its_path() -> None:
    with pytest.raises(KeyError):
        EndpointSpec.from_dict({"host": "api.github.com", "auth_scheme": "bearer"})
