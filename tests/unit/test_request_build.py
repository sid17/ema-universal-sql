"""``build_request`` — where ``inject_into`` stops being documentation.

The assertion this file exists for is
:func:`test_swapping_a_placement_changes_the_call`: before this phase the
placement metadata was carried from YAML through the planner and then dropped,
so ``path`` and ``query`` were interchangeable and nothing could tell. Every
other test here is about one source's real request shape.
"""

import base64

import pytest

from src.connectors.base import CapabilityModel, FetchRequest
from src.connectors.request import REDACTED, EndpointSpec, build_request
from tests.unit.catalog_fixture import (
    GITHUB_CAPABILITIES,
    GITHUB_ENDPOINT,
    JIRA_CAPABILITIES,
    JIRA_ENDPOINT,
)

GITHUB = CapabilityModel.from_dict(GITHUB_CAPABILITIES)
JIRA = CapabilityModel.from_dict(JIRA_CAPABILITIES)

#: Self-describing, and never a real credential — see scripts/seed.py.
TOKEN = "mock_github_tenant_acme_Zq8xk1"


def github_request(**overrides) -> FetchRequest:
    fields = {
        "tenant_id": "tenant_acme",
        "entitlement_scope": "support:alice",
        "predicates": {"repo": "ema/core", "state": "open"},
        "limit": 5,
    }
    return FetchRequest(**(fields | overrides))


def jira_request(**overrides) -> FetchRequest:
    fields = {
        "tenant_id": "tenant_acme",
        "entitlement_scope": "support:alice",
        "predicates": {"status": "In Progress", "assignee": "alice"},
        "limit": 5,
    }
    return FetchRequest(**(fields | overrides))


# --- the two real shapes ----------------------------------------------------


def test_github_renders_a_path_parameter_and_query_filters() -> None:
    outbound = build_request(GITHUB_ENDPOINT, GITHUB, github_request(), TOKEN)

    assert outbound.method == "GET"
    assert outbound.host == "api.github.com"
    # `repo` went into the PATH. `state` went into the QUERY. That split is the
    # capability model's, not this test's.
    assert outbound.path == "/repos/ema/core/pulls"
    assert outbound.query == {"state": "open", "per_page": "5"}
    assert outbound.headers["Accept"] == "application/vnd.github+json"
    assert outbound.headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_jira_composes_every_filter_into_one_jql_expression() -> None:
    outbound = build_request(JIRA_ENDPOINT, JIRA, jira_request(), TOKEN)

    assert outbound.path == "/rest/api/3/search"
    # One endpoint, one expression — no per-field query parameter, which is what
    # real Jira search does and what `inject_into: jql` exists to express.
    assert outbound.query["jql"] == 'assignee = "alice" AND status = "In Progress"'
    assert outbound.query["maxResults"] == "5"
    # A real Jira client always sends startAt, first page included.
    assert outbound.query["startAt"] == "0"


def test_a_range_operator_survives_into_the_expression() -> None:
    """Jira's `updated` is the only column declaring non-equality ops.

    Everywhere else in this system a range operator is *declared* and then
    checked; this is the one place it is actually rendered.
    """
    request = jira_request(predicates={"updated": (">=", "2026-09-01T00:00:00Z")})

    outbound = build_request(JIRA_ENDPOINT, JIRA, request, TOKEN)

    assert outbound.query["jql"] == 'updated >= "2026-09-01T00:00:00Z"'


def test_the_expression_is_stable_regardless_of_predicate_order() -> None:
    """The artifact must not churn when the planner reorders an unchanged plan."""
    forwards = build_request(JIRA_ENDPOINT, JIRA, jira_request(), TOKEN)
    backwards = build_request(
        JIRA_ENDPOINT,
        JIRA,
        jira_request(predicates={"assignee": "alice", "status": "In Progress"}),
        TOKEN,
    )

    assert forwards.query["jql"] == backwards.query["jql"]


# --- the assertion that was impossible before this phase --------------------


def test_swapping_a_placement_changes_the_call() -> None:
    """Move `repo` from the path to the query and the built request follows.

    Until `build_request` existed, `inject_into` was carried one hop by the
    planner and then dropped: the mock filtered rows with a dict comprehension,
    so this edit changed nothing anywhere and the whole suite still passed.
    """
    swapped = dict(GITHUB_CAPABILITIES)
    swapped["key_columns"] = dict(swapped["key_columns"]) | {
        "repo": {
            "require": "optional",
            "ops": ["="],
            "option": {"inject_into": "query", "field": "repo"},
        }
    }
    endpoint = EndpointSpec(host="api.github.com", path_template="/pulls", auth_scheme="bearer")

    outbound = build_request(endpoint, CapabilityModel.from_dict(swapped), github_request(), TOKEN)

    assert outbound.path == "/pulls"
    assert outbound.query["repo"] == "ema/core"


# --- pagination: strategy ⊕ placement, both halves --------------------------


def test_a_cursor_is_sent_verbatim_and_omitted_on_the_first_page() -> None:
    first = build_request(GITHUB_ENDPOINT, GITHUB, github_request(), TOKEN)
    assert "cursor" not in first.query

    token = "eyJ2IjoxLCJzIjoiY3Vyc29yIiwibyI6NX0"
    later = build_request(GITHUB_ENDPOINT, GITHUB, github_request(page=token), TOKEN)
    assert later.query["cursor"] == token


def test_an_offset_page_is_sent_as_an_integer_start_at() -> None:
    """The opaque token is ours; `startAt` is what the source understands."""
    token = "eyJ2IjoxLCJzIjoib2Zmc2V0IiwibyI6NX0"

    outbound = build_request(JIRA_ENDPOINT, JIRA, jira_request(page=token), TOKEN)

    assert outbound.query["startAt"] == "5"


def test_a_foreign_cursor_is_refused_before_it_is_sent() -> None:
    """A GitHub cursor handed to Jira would be a valid integer offset into the
    wrong dataset — real rows for the wrong question."""
    github_token = "eyJ2IjoxLCJzIjoiY3Vyc29yIiwibyI6NX0"

    with pytest.raises(ValueError, match="issued by the 'cursor' strategy"):
        build_request(JIRA_ENDPOINT, JIRA, jira_request(page=github_token), TOKEN)


def test_the_page_size_is_clamped_to_what_the_source_will_return() -> None:
    outbound = build_request(GITHUB_ENDPOINT, GITHUB, github_request(limit=5000), TOKEN)

    assert outbound.query["per_page"] == str(GITHUB.pagination.page_size)


# --- credentials ------------------------------------------------------------


def test_bearer_carries_the_resolved_credential() -> None:
    outbound = build_request(GITHUB_ENDPOINT, GITHUB, github_request(), TOKEN)

    assert outbound.headers["Authorization"] == f"Bearer {TOKEN}"


def test_basic_base64_encodes_the_credential_whole() -> None:
    """A Jira credential IS `email:api_token`; encoding only a token would
    render a header no live adapter could send."""
    credential = "svc@ema.co:mock_jira_tenant_acme_Zq8xk1"

    outbound = build_request(JIRA_ENDPOINT, JIRA, jira_request(), credential)

    scheme, encoded = outbound.headers["Authorization"].split(" ", 1)
    assert scheme == "Basic"
    assert base64.b64decode(encoded).decode() == credential


def test_no_credential_means_no_authorization_header() -> None:
    outbound = build_request(GITHUB_ENDPOINT, GITHUB, github_request())

    assert "Authorization" not in outbound.headers


def test_redaction_hides_the_secret_but_keeps_the_scheme() -> None:
    outbound = build_request(GITHUB_ENDPOINT, GITHUB, github_request(), TOKEN)

    redacted = outbound.redacted()

    assert redacted.headers["Authorization"] == f"Bearer {REDACTED}"
    # The real value stays on the original: redaction is a property of
    # rendering, so the credential still reaches the transport as it would live.
    assert outbound.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(redacted.headers)
    assert TOKEN not in redacted.url


def test_redaction_leaves_everything_else_identical() -> None:
    outbound = build_request(JIRA_ENDPOINT, JIRA, jira_request(), TOKEN)

    redacted = outbound.redacted()

    assert redacted.url == outbound.url
    assert redacted.query == outbound.query


# --- refusals: LAW 4, every one of them loud --------------------------------


def test_a_predicate_with_no_declared_placement_is_refused() -> None:
    request = github_request(predicates={"repo": "ema/core", "title": "anything"})

    with pytest.raises(ValueError, match="declares no placement"):
        build_request(GITHUB_ENDPOINT, GITHUB, request, TOKEN)


def test_a_range_operator_on_an_equality_only_placement_is_refused() -> None:
    """A query parameter has nowhere to put a `>`. Dropping it silently would
    return rows the caller did not ask for."""
    request = github_request(predicates={"repo": "ema/core", "state": (">", "open")})

    with pytest.raises(ValueError, match="can only carry equality"):
        build_request(GITHUB_ENDPOINT, GITHUB, request, TOKEN)


def test_a_missing_path_parameter_refuses_to_emit_a_url_with_a_hole() -> None:
    request = github_request(predicates={"state": "open"})

    with pytest.raises(ValueError, match="needs repo"):
        build_request(GITHUB_ENDPOINT, GITHUB, request, TOKEN)


def test_a_jql_placement_against_an_endpoint_without_one_is_refused() -> None:
    endpoint = EndpointSpec(host="example.test", path_template="/search", auth_scheme="bearer")

    with pytest.raises(ValueError, match="no expression_param"):
        build_request(endpoint, JIRA, jira_request(), TOKEN)


def test_a_quote_inside_a_value_cannot_break_out_of_the_expression() -> None:
    request = jira_request(predicates={"status": 'In "Progress"'})

    outbound = build_request(JIRA_ENDPOINT, JIRA, request, TOKEN)

    assert outbound.query["jql"] == 'status = "In \\"Progress\\""'
