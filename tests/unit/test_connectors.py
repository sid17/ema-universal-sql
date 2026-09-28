"""Both adapters: capability enforcement, predicate pushdown, and the response shape."""

import pytest

from src.connectors.base import FetchRequest
from src.connectors.errors import FailureMode
from src.models.errors import ApiError, ErrorCode

ACME = "tenant_acme"
SCOPE = "support"


def gh_request(**kwargs) -> FetchRequest:
    """GitHub always needs `repo` — it is a required path parameter."""
    predicates = {"repo": "ema/core", **kwargs.pop("predicates", {})}
    return FetchRequest(
        tenant_id=ACME, entitlement_scope=SCOPE, predicates=predicates, **kwargs
    )


def jira_request(**kwargs) -> FetchRequest:
    return FetchRequest(tenant_id=ACME, entitlement_scope=SCOPE, **kwargs)


# --- capability enforcement ------------------------------------------------


async def test_required_predicate_cannot_be_omitted(github):
    """GitHub has no endpoint spanning every repo, so `repo` is not optional."""
    with pytest.raises(ApiError) as raised:
        await github.fetch(FetchRequest(tenant_id=ACME, entitlement_scope=SCOPE))
    assert "requires a predicate on repo" in raised.value.message


async def test_jira_has_no_required_predicates(jira):
    """The asymmetry that proves the contract handles both requirements."""
    response = await jira.fetch(jira_request())
    assert len(response.rows) == 20


async def test_undeclared_column_is_rejected_not_ignored(github):
    """Silently dropping it would return rows the caller did not ask for.

    In Phase 2 the dropped predicate could be the RLS filter, which makes a
    silent drop a data leak rather than a bug.
    """
    with pytest.raises(ApiError) as raised:
        await github.fetch(gh_request(predicates={"issue_key": "SUP-12"}))
    assert raised.value.code is ErrorCode.ENTITLEMENT_DENIED
    assert "cannot filter 'issue_key'" in raised.value.message


async def test_undeclared_operator_is_rejected(github):
    """GitHub filters `state` by equality only; `>` is not expressible upstream."""
    with pytest.raises(ApiError) as raised:
        await github.fetch(gh_request(predicates={"state": (">", "open")}))
    assert raised.value.code is ErrorCode.ENTITLEMENT_DENIED


async def test_jira_supports_range_operators_on_updated(jira):
    """The one column with range support — the reason `supports` takes two args."""
    response = await jira.fetch(jira_request(predicates={"updated": (">", "2026-09-27T00:00:00Z")}))
    assert response.rows
    assert all(r["updated"] > "2026-09-27T00:00:00Z" for r in response.rows)


async def test_jira_rejects_a_range_operator_on_an_equality_column(jira):
    with pytest.raises(ApiError) as raised:
        await jira.fetch(jira_request(predicates={"status": (">", "Done")}))
    assert raised.value.code is ErrorCode.ENTITLEMENT_DENIED


async def test_validation_happens_before_any_token_is_spent(github, limiter):
    """An invalid request must not cost budget."""
    with pytest.raises(ApiError):
        await github.fetch(gh_request(predicates={"issue_key": "SUP-12"}))
    assert limiter.consumed == []


# --- predicate pushdown actually filters -----------------------------------


async def test_predicates_filter_exactly(github):
    response = await github.fetch(gh_request(predicates={"state": "open"}))
    assert response.rows
    assert all(r["repo"] == "ema/core" and r["state"] == "open" for r in response.rows)


async def test_the_canonical_github_predicates_return_the_expected_set(github):
    """Open PRs in ema/core — the GitHub half of the canonical query.

    14, not the 8 rows the full query returns: this is one source's contribution
    *before* the join to In Progress issues removes the rest. The two numbers
    differing is the point of federating at all.
    """
    response = await github.fetch(gh_request(predicates={"state": "open"}))
    assert len(response.rows) == 14


async def test_the_canonical_jira_predicates_return_the_expected_set(jira):
    """In Progress issues — the Jira half, again pre-join."""
    response = await jira.fetch(jira_request(predicates={"status": "In Progress"}))
    assert len(response.rows) == 9


async def test_rls_style_predicate_shrinks_the_set(jira):
    """The shape Phase 2's compiled RLS predicate will take."""
    alice = await jira.fetch(
        jira_request(predicates={"status": "In Progress", "assignee": "alice"})
    )
    bob = await jira.fetch(
        jira_request(predicates={"status": "In Progress", "assignee": "bob"})
    )
    carol = await jira.fetch(
        jira_request(predicates={"status": "In Progress", "assignee": "carol"})
    )
    assert len(alice.rows) == 3
    assert len(bob.rows) == 1
    assert len(carol.rows) == 1  # SUP-31 exists; its PR is closed, so the JOIN empties it


async def test_multiple_predicates_are_conjunctive(github):
    response = await github.fetch(
        gh_request(predicates={"state": "open", "author": "ana-dev"})
    )
    assert all(r["state"] == "open" and r["author"] == "ana-dev" for r in response.rows)
    assert len(response.rows) == 3


async def test_a_predicate_matching_nothing_returns_empty_not_an_error(github):
    """`empty` is a legitimate outcome, distinct from `error` (DoD §4)."""
    response = await github.fetch(gh_request(predicates={"author": "nobody"}))
    assert response.rows == []
    assert response.has_more is False
    assert response.served == "live"


# --- projection ------------------------------------------------------------


async def test_projection_narrows_columns(github):
    response = await github.fetch(gh_request(projection=["title", "author"]))
    assert all(set(r) == {"title", "author"} for r in response.rows)


async def test_empty_projection_means_every_column(github):
    """A caller that has not chosen columns must not be handed zero columns."""
    response = await github.fetch(gh_request(projection=[]))
    assert "issue_key" in response.rows[0]


# --- the response contract -------------------------------------------------


async def test_live_response_is_fully_populated(github, fake_clock):
    response = await github.fetch(gh_request(limit=5))
    assert response.served == "live"
    assert response.revalidated is False
    assert response.etag is not None
    assert response.has_more is True
    assert response.next_cursor is not None
    assert response.fetched_at == pytest.approx(fake_clock() / 1000)


async def test_etag_is_stable_for_stable_data(github, second_github):
    """What makes the 304 path work at all — computed twice, not read twice.

    The two fetches go through adapters with SEPARATE Redis instances, so the
    second genuinely recomputes the ETag instead of reading back the first
    one's. An earlier version of this test reused one adapter, which meant the
    second call was a cache hit returning the stored value — equal ETags were
    guaranteed by construction and the assertion could not fail even if `_etag`
    returned a random value each time.
    """
    first = await github.fetch(gh_request(limit=5))
    second = await second_github.fetch(gh_request(limit=5))

    assert first.served == "live"
    assert second.served == "live", "the second adapter must not share a cache"
    assert first.etag == second.etag
    assert first.etag is not None


async def test_etag_changes_when_the_rows_change(github):
    a = await github.fetch(gh_request(predicates={"state": "open"}))
    b = await github.fetch(gh_request(predicates={"state": "closed"}))
    assert a.etag != b.etag


async def test_pagination_is_wired_through_fetch(github):
    """GATE test_pagination, exercised end-to-end rather than on the strategy alone."""
    first = await github.fetch(gh_request(limit=5))
    assert len(first.rows) == 5
    assert first.has_more is True

    second = await github.fetch(gh_request(limit=5, page=first.next_cursor))
    assert len(second.rows) == 5
    numbers_a = {r["number"] for r in first.rows}
    numbers_b = {r["number"] for r in second.rows}
    assert numbers_a.isdisjoint(numbers_b)


async def test_the_two_adapters_use_different_pagination_strategies(github, jira):
    """Cursor for GitHub, offset for Jira — both proven by a cross-feed rejection."""
    assert github.capabilities().pagination.strategy == "cursor"
    assert jira.capabilities().pagination.strategy == "offset"
    github_cursor = (await github.fetch(gh_request(limit=5))).next_cursor
    with pytest.raises(ValueError, match="was issued by the 'cursor' strategy"):
        await jira.fetch(jira_request(limit=5, page=github_cursor))


# --- grants and budgets ----------------------------------------------------


async def test_a_tenant_without_a_grant_is_refused(github, control_plane):
    """Never a fallback to a default credential — that would fail open."""
    with pytest.raises(ApiError) as raised:
        await github.fetch(
            FetchRequest(tenant_id="tenant_globex", entitlement_scope=SCOPE,
                         predicates={"repo": "ema/core"})
        )
    assert raised.value.code is ErrorCode.CONNECTOR_NOT_ENABLED


async def test_a_disabled_grant_is_refused(github, control_plane):
    control_plane.grants[ACME][0]["enabled"] = False
    with pytest.raises(ApiError) as raised:
        await github.fetch(gh_request())
    assert raised.value.code is ErrorCode.CONNECTOR_NOT_ENABLED


async def test_a_missing_budget_is_refused_rather_than_unlimited(github, control_plane):
    """No budget row must not mean "no limit" — that is the fail-open direction."""
    del control_plane.rate_limits[(ACME, "github")]
    with pytest.raises(ApiError) as raised:
        await github.fetch(gh_request())
    assert raised.value.code is ErrorCode.CONNECTOR_NOT_ENABLED


async def test_the_tenants_own_secret_is_resolved(github, control_plane):
    await github.fetch(gh_request())
    assert ("get_secret", ("tenant_acme/github",)) in control_plane.calls


# --- forced failures -------------------------------------------------------


async def test_forced_timeout_raises_the_timeout_code(github):
    github.fail_next(FailureMode.TIMEOUT)
    with pytest.raises(ApiError) as raised:
        await github.fetch(gh_request())
    assert raised.value.code is ErrorCode.SOURCE_TIMEOUT
    assert raised.value.http == 504


async def test_forced_auth_failure_raises_the_auth_code(github):
    github.fail_next(FailureMode.AUTH)
    with pytest.raises(ApiError) as raised:
        await github.fetch(gh_request())
    assert raised.value.code is ErrorCode.CONNECTOR_AUTH_ERROR


async def test_a_forced_failure_applies_once(github):
    """One-shot, so a demo step cannot poison every later request."""
    github.fail_next(FailureMode.TIMEOUT)
    with pytest.raises(ApiError):
        await github.fetch(gh_request())
    assert (await github.fetch(gh_request())).served == "live"


async def test_a_forced_failure_spends_no_token(github, limiter):
    github.fail_next(FailureMode.TIMEOUT)
    with pytest.raises(ApiError):
        await github.fetch(gh_request())
    assert limiter.consumed == []


# --- health ----------------------------------------------------------------


async def test_health_reports_the_dataset_size(github, jira):
    assert github.health() == {
        "connector": "github", "resource": "pull_requests", "status": "ok", "rows": 20,
    }
    assert jira.health()["connector"] == "jira"


# --- isolation of the dataset ----------------------------------------------


async def test_filtering_does_not_mutate_the_shared_dataset(github):
    """The adapter works on a copy; the module constant must survive."""
    from src.connectors.mock_data import GITHUB_PULL_REQUESTS

    await github.fetch(gh_request(predicates={"state": "open"}, projection=["title"]))
    assert len(GITHUB_PULL_REQUESTS) == 20
    assert "issue_key" in GITHUB_PULL_REQUESTS[0]


# --- projection is validated, not silently trimmed --------------------------


async def test_a_projection_naming_an_unknown_column_is_rejected(github):
    """Regression: unknown columns used to be silently dropped.

    Asking GitHub for `reporter_email` (a Jira column) returned rows without it
    and no error. That is invisible at this boundary and becomes wrong results
    in Phase 2: DoD §4 non-negotiable #2 has the engine fetch
    `projection ∪ every WHERE/ORDER BY column` and then re-apply predicates
    authoritatively. A silently dropped column means re-filtering on data that
    was never fetched, discarding rows the caller was entitled to.
    """
    with pytest.raises(ApiError) as raised:
        await github.fetch(gh_request(projection=["title", "reporter_email"]))
    assert raised.value.code is ErrorCode.ENTITLEMENT_DENIED
    assert "reporter_email" in raised.value.message


async def test_projection_rejection_names_the_available_columns(github):
    """A caller that guessed wrong should be told what it could have asked for."""
    with pytest.raises(ApiError) as raised:
        await github.fetch(gh_request(projection=["nonexistent"]))
    assert "issue_key" in raised.value.message


async def test_a_valid_projection_still_narrows(github):
    response = await github.fetch(gh_request(projection=["title", "author"]))
    assert all(set(r) == {"title", "author"} for r in response.rows)


async def test_projection_is_validated_before_a_token_is_spent(github, limiter):
    with pytest.raises(ApiError):
        await github.fetch(gh_request(projection=["reporter_email"]))
    assert limiter.consumed == []


async def test_jira_accepts_the_column_github_rejects(jira):
    """The columns are per-connector, read from each one's seeded capabilities."""
    response = await jira.fetch(jira_request(projection=["key", "reporter_email"]))
    assert all(set(r) == {"key", "reporter_email"} for r in response.rows)
