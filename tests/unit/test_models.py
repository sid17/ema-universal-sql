"""The envelope is a locked contract — these tests are its guard rail."""

from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from src.models.context import UserContext
from src.models.envelope import ConnectorBudget, QueryEnvelope, SourceOutcome
from src.models.request import QueryRequest

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"


def test_envelope_shell_serialises_with_empty_rows_and_a_trace_id():
    body = QueryEnvelope(trace_id=TRACE_ID).model_dump()
    assert body["rows"] == []
    assert body["columns"] == []
    assert body["trace_id"] == TRACE_ID
    assert body["freshness_ms"] is None
    assert body["partial"] is False


def test_join_status_defaults_to_not_applicable():
    assert QueryEnvelope(trace_id=TRACE_ID).join_status == "n/a"


def test_trace_id_is_required():
    with pytest.raises(ValidationError):
        QueryEnvelope()


def test_mutable_defaults_are_not_shared_between_envelopes():
    first = QueryEnvelope(trace_id=TRACE_ID)
    first.rows.append([1])
    assert QueryEnvelope(trace_id=TRACE_ID).rows == []


def test_invalid_source_outcome_state_raises():
    with pytest.raises(ValidationError):
        SourceOutcome(connector="github", state="exploded", served="live")


def test_invalid_source_outcome_served_raises():
    with pytest.raises(ValidationError):
        SourceOutcome(connector="github", state="ok", served="maybe")


def test_partial_envelope_must_not_carry_a_cursor():
    with pytest.raises(ValidationError):
        QueryEnvelope(trace_id=TRACE_ID, partial=True, next_cursor="opaque-cursor")


def test_partial_envelope_with_no_cursor_is_valid():
    envelope = QueryEnvelope(trace_id=TRACE_ID, partial=True)
    assert envelope.next_cursor is None


def test_complete_envelope_may_carry_a_cursor():
    envelope = QueryEnvelope(trace_id=TRACE_ID, partial=False, next_cursor="opaque-cursor")
    assert envelope.next_cursor == "opaque-cursor"


def test_rate_limit_status_holds_connector_budgets():
    envelope = QueryEnvelope(
        trace_id=TRACE_ID,
        rate_limit_status={"github": ConnectorBudget(remaining=7, throttled=False)},
        sources=[SourceOutcome(connector="github", state="ok", served="cache")],
    )
    dumped = envelope.model_dump()
    assert dumped["rate_limit_status"]["github"] == {"remaining": 7, "throttled": False}
    assert dumped["sources"][0]["served"] == "cache"


def test_user_context_is_frozen():
    ctx = UserContext(tenant_id="tenant_acme", user_id="alice", roles=["support"], raw_claims={})
    with pytest.raises(FrozenInstanceError):
        ctx.tenant_id = "tenant_other"


def test_query_request_defaults():
    request = QueryRequest(sql="SELECT 1")
    assert request.max_staleness_ms == 60_000
    assert request.cursor is None
