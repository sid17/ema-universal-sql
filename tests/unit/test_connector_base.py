"""The connector contract: built from seeded data, and not instantiable bare."""

import pytest

from src.connectors.base import (
    AdapterResponse,
    BaseConnectorAdapter,
    CapabilityModel,
    FetchRequest,
    RequestOption,
)

# The shape `connectors.capabilities` actually stores, as seeded from YAML.
GITHUB_CAPABILITIES = {
    "columns": ["title", "author", "repo", "state", "issue_key", "created_at", "updated_at"],
    "key_columns": {
        "repo": {
            "require": "required",
            "ops": ["="],
            "option": {"inject_into": "path", "field": "repo"},
        },
        "state": {
            "require": "optional",
            "ops": ["="],
            "option": {"inject_into": "query", "field": "state"},
        },
        "author": {
            "require": "optional",
            "ops": ["="],
            "option": {"inject_into": "query", "field": "author"},
        },
    },
    "sortable": ["created_at", "updated_at"],
    "pagination": {
        "strategy": "cursor",
        "page_size": 100,
        "token_option": {"inject_into": "query", "field": "cursor"},
        "size_option": {"inject_into": "query", "field": "per_page"},
        "stop": "returned<page_size",
    },
}


def test_capability_model_round_trips_a_seeded_dict():
    model = CapabilityModel.from_dict(GITHUB_CAPABILITIES)
    assert model.columns[0] == "title"
    assert model.sortable == ("created_at", "updated_at")
    assert model.pagination.strategy == "cursor"
    assert model.pagination.page_size == 100
    assert model.pagination.token_option == RequestOption("query", "cursor")


def test_capability_carries_both_predicate_support_and_placement():
    """The Card 2 point: knowing *what* is filterable is useless without *where*."""
    model = CapabilityModel.from_dict(GITHUB_CAPABILITIES)
    repo = model.key_columns["repo"]
    state = model.key_columns["state"]
    # Same operator, different injection site — the reason placement is separate.
    assert repo.ops == state.ops == ("=",)
    assert repo.option.inject_into == "path"
    assert state.option.inject_into == "query"


def test_required_columns_are_distinguished_from_optional():
    model = CapabilityModel.from_dict(GITHUB_CAPABILITIES)
    assert model.required_columns() == ("repo",)


def test_supports_answers_column_and_operator_together():
    model = CapabilityModel.from_dict(GITHUB_CAPABILITIES)
    assert model.supports("state", "=") is True
    # Right column, wrong operator — GitHub cannot express `state > x`.
    assert model.supports("state", ">") is False
    # Column the source does not filter on at all.
    assert model.supports("reporter_email", "=") is False


def test_operators_come_from_the_seed_not_from_the_adapter():
    """An operator list the seed does not declare must not appear.

    Guards the ADR-003 decision that capabilities are built from the seeded
    dict: if an adapter could widen its own operator set, Phase 2's planner
    would push down a predicate the source cannot honour.
    """
    narrowed = {
        **GITHUB_CAPABILITIES,
        "key_columns": {
            "repo": {
                "require": "required",
                "ops": ["="],
                "option": {"inject_into": "path", "field": "repo"},
            },
        },
    }
    model = CapabilityModel.from_dict(narrowed)
    assert model.supports("state", "=") is False


def test_base_adapter_cannot_be_instantiated():
    with pytest.raises(TypeError):
        BaseConnectorAdapter()


def test_subclass_missing_fetch_cannot_be_instantiated():
    class Incomplete(BaseConnectorAdapter):
        connector_type = "incomplete"

        def capabilities(self):
            return CapabilityModel.from_dict(GITHUB_CAPABILITIES)

        def health(self):
            return {"status": "ok"}

    with pytest.raises(TypeError, match="fetch"):
        Incomplete()


def test_adapter_response_defaults_to_a_terminal_page():
    """A response that forgets to say it has more must not claim it does."""
    response = AdapterResponse(rows=[], fetched_at=1.0, served="live")
    assert response.has_more is False
    assert response.next_cursor is None
    assert response.revalidated is False


def test_fetch_request_requires_tenant_and_entitlement_scope():
    """ADR-025: neither segment may be defaulted away."""
    with pytest.raises(TypeError):
        FetchRequest(tenant_id="tenant_acme")
    request = FetchRequest(tenant_id="tenant_acme", entitlement_scope="support")
    assert request.limit == 100
    assert request.page is None
