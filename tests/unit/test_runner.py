"""The pipeline: stage order, spans, stats and the audit row.

This file asserts *sequence*, because sequence is the design. `parse → entitle →
plan` is what puts the RLS predicate into the tree before the pushdown split; the
opposite order returns identical rows by post-filtering, which is the one outcome
`02-DEFINITION-OF-DONE.md` §4 forbids.
"""

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.execution.assemble import ResultAssembler
from src.governance.audit import AuditLogger
from src.models.errors import ApiError, ErrorCode, InvalidQueryError
from src.models.request import QueryRequest
from src.observability.tracing import ELAPSED_MS_ATTRIBUTE, reset_tracing
from src.pipeline.registry import ConnectorRegistry
from src.pipeline.runner import QueryPipelineRunner
from tests.unit.conftest import (
    CANONICAL_SQL,
    CLS_DEMO_SQL,
    GITHUB_CAPABILITIES,
    JIRA_CAPABILITIES,
    persona,
)
from tests.unit.test_audit import FakePool
from tests.unit.test_entitlement import CLS_REPORTER, DENY_JIRA, RLS_ASSIGNEE

STAGES = ("parse", "entitlement", "plan", "federation", "assemble")


class FakeRepository:
    """The five control-plane reads the pipeline makes.

    Wraps the connector fake so one object answers everything the runner needs,
    which is also how `ControlPlaneRepository` presents itself in production.
    """

    def __init__(self, control_plane, policies) -> None:
        self._control_plane = control_plane
        self.policies = list(policies)
        self.capabilities = {
            "github": {"capabilities": GITHUB_CAPABILITIES},
            "jira": {"capabilities": JIRA_CAPABILITIES},
        }

    def get_capabilities(self, connector_type):
        return self.capabilities.get(connector_type)

    def get_policies(self, tenant_id, connectors, resources):
        return self.policies

    def get_tenant_connectors(self, tenant_id):
        return self._control_plane.get_tenant_connectors(tenant_id)

    def get_rate_limit_policy(self, tenant_id, connector_type):
        return self._control_plane.get_rate_limit_policy(tenant_id, connector_type)

    def get_secret(self, secret_ref):
        return self._control_plane.get_secret(secret_ref)

    def get_tenant(self, tenant_id):
        return self._control_plane.get_tenant(tenant_id)


@pytest.fixture
def pool() -> FakePool:
    return FakePool()


@pytest.fixture
def runner(cache, limiter, secrets, control_plane, fake_clock, pool):
    def _build(policies=(RLS_ASSIGNEE, CLS_REPORTER), deadline_ms=5000):
        repository = FakeRepository(control_plane, policies)
        registry = ConnectorRegistry(repository, cache, limiter, secrets)
        return QueryPipelineRunner(
            registry=registry,
            repository=repository,
            assembler=ResultAssembler(limiter, repository, fake_clock),
            audit=AuditLogger(pool),
            deadline_ms=deadline_ms,
        )

    return _build


@pytest.fixture
def spans():
    """Spans exported inline, so an assertion right after the call sees them."""
    exporter = InMemorySpanExporter()
    provider = reset_tracing()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter
    reset_tracing()


async def run(runner_factory, sql=CANONICAL_SQL, user_id="alice", **kwargs):
    return await runner_factory(**kwargs).run(
        QueryRequest(sql=sql), persona(user_id), "trace-xyz"
    )


# --- the end-to-end flow ----------------------------------------------------


async def test_the_canonical_query_runs_end_to_end(runner):
    env = await run(runner)
    assert len(env.rows) == 3
    assert [c.name for c in env.columns] == ["title", "author", "key", "status"]
    assert env.join_status == "complete"
    assert env.partial is False
    assert env.trace_id == "trace-xyz"


@pytest.mark.parametrize(("user_id", "expected"), [("alice", 3), ("bob", 1), ("carol", 0)])
async def test_the_same_sql_gives_each_persona_their_own_answer(runner, user_id, expected):
    """**Hard part 1, through the whole pipeline.** Same query text, different
    plan, different answer — and the difference was compiled in, not filtered out."""
    env = await run(runner, user_id=user_id)
    assert len(env.rows) == expected


async def test_cls_masks_the_email_end_to_end(runner):
    env = await run(runner, sql=CLS_DEMO_SQL)
    assert [c.name for c in env.columns if c.masked] == ["reporter_email"]
    assert "@acme.com" not in str(env.rows)


# --- stage order and spans --------------------------------------------------


async def test_every_stage_emits_a_span_with_its_elapsed_time(runner, spans):
    """Front-loaded observability: each stage was instrumented as it was written,
    so Phase 4 renders a waterfall without re-reading five modules to find the
    boundaries."""
    await run(runner)
    emitted = {span.name: span for span in spans.get_finished_spans()}
    for name in STAGES:
        assert name in emitted, f"no span for stage {name}"
        assert emitted[name].attributes[ELAPSED_MS_ATTRIBUTE] >= 0


async def test_the_stages_ran_in_the_designed_order(runner, spans):
    """**`parse -> entitle -> plan` is the design, not a preference.**

    Entitlement must be injected BEFORE the pushdown split, or the RLS predicate
    cannot ride pushdown to the source. Spans end in completion order, which for
    sequential stages is the order they ran.
    """
    await run(runner)
    order = [s.name for s in spans.get_finished_spans() if s.name in STAGES]
    assert order == list(STAGES)


async def test_stage_timings_reach_the_envelope(runner):
    """The trace and the envelope must not disagree about where time went."""
    env = await run(runner)
    for name in STAGES:
        assert f"{name}_ms" in env.stats
    assert set(env.stats["connector_ms"]) == {"github", "jira"}


# --- the audit row ----------------------------------------------------------


async def test_one_audit_row_per_query(runner, pool):
    await run(runner)
    assert len(pool.writes) == 1
    _, params = pool.writes[0]
    assert params[0] == "tenant_acme"
    assert params[1] == "alice"
    assert sorted(params[3]) == ["github", "jira"]
    assert params[4] == 3
    assert params[5] == "trace-xyz"


async def test_the_audit_row_carries_no_literal_values(runner, pool):
    """**ADR-033.** The logging layer must not defeat the masking layer."""
    await run(runner, sql=CLS_DEMO_SQL)
    query_text = pool.writes[0][1][2]
    for literal in ("ema/core", "open", "In Progress", "alice"):
        assert literal not in query_text
    # ...but the SHAPE survives, which is what an access trail is for.
    assert "reporter_email" in query_text
    assert "JOIN" in query_text


async def test_an_empty_result_is_still_audited(runner, pool):
    await run(runner, user_id="carol")
    assert pool.writes[0][1][4] == 0


async def test_a_refused_query_writes_no_audit_row(runner, pool):
    """Nothing was accessed, so there is nothing to record. A row here would
    make the access trail claim a read that never happened."""
    with pytest.raises(InvalidQueryError):
        await run(runner, sql="SELECT * FROM jira.issues issue")
    assert pool.writes == []


# --- the refusals, through the route-level path -----------------------------


async def test_malformed_sql_is_a_400(runner):
    with pytest.raises(InvalidQueryError) as raised:
        await run(runner, sql="SELECT FROM WHERE")
    assert raised.value.http == 400


async def test_an_auditor_is_denied_but_a_support_user_is_not(runner):
    policies = (RLS_ASSIGNEE, CLS_REPORTER, DENY_JIRA)
    assert len((await run(runner, policies=policies)).rows) == 3

    with pytest.raises(ApiError) as raised:
        await runner(policies=policies).run(
            QueryRequest(sql=CANONICAL_SQL), persona("alice", "auditor"), "t"
        )
    assert raised.value.code is ErrorCode.ENTITLEMENT_DENIED
    assert raised.value.http == 403


async def test_default_deny_is_an_empty_200_not_a_403(runner):
    """The other half of ADR-031's split: `contractor` matches neither the
    support allow nor the auditor deny, so the answer is empty and successful."""
    env = await runner(policies=(RLS_ASSIGNEE, CLS_REPORTER, DENY_JIRA)).run(
        QueryRequest(sql=CANONICAL_SQL), persona("dave", "contractor"), "t"
    )
    assert env.rows == []
    assert env.partial is False


async def test_an_unseeded_connector_makes_its_table_unknown(runner, control_plane):
    """Half a seeded control plane should make the unseeded connector unknown,
    not take the service down. `make up` runs before `make seed`."""
    built = runner()
    built._registry._repository.capabilities.pop("jira")
    with pytest.raises(InvalidQueryError) as raised:
        await built.run(QueryRequest(sql=CANONICAL_SQL), persona("alice"), "t")
    assert raised.value.detail == "UnknownTable"


# --- the staleness knob (DoD §2 hard part 4) --------------------------------


async def test_the_staleness_knob_flips_live_to_cache(runner):
    """Same query twice: `max_staleness_ms=0` forces a live fetch, then 60000
    accepts the cached one. No token is spent on the hit."""
    built = runner()
    live = await built.run(
        QueryRequest(sql=CANONICAL_SQL, max_staleness_ms=0), persona("alice"), "t"
    )
    assert {s.served for s in live.sources} == {"live"}
    assert set(live.stats["connector_ms"]) == {"github", "jira"}

    cached = await built.run(
        QueryRequest(sql=CANONICAL_SQL, max_staleness_ms=60_000), persona("alice"), "t"
    )
    assert {s.served for s in cached.sources} == {"cache"}
    assert cached.stats["connector_ms"] == {}
    assert len(cached.rows) == len(live.rows)

    # No token spent on the hit: the budget did not go DOWN.
    for connector, budget in cached.rate_limit_status.items():
        assert budget.remaining >= live.rate_limit_status[connector].remaining
