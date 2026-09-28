"""Stage 5b — where the answer came from, and how good it is.

Split from `test_assemble.py` when that file crossed LAW 1's 400-line
decompose threshold. The seam is by subject: that file asserts the **shape** a
caller gets back (columns, masking, pages, cursors); this one asserts the
**provenance** attached to it — freshness, the three outcomes staying distinct,
join status, remaining budget and the stage timings.

`02-DEFINITION-OF-DONE.md` §4's third non-negotiable lives here: empty, partial
and error must be three shapes a caller can tell apart without reading prose.
"""

import pytest
from prometheus_client import REGISTRY

from src.connectors.base import AdapterResponse
from src.connectors.errors import FailureMode
from src.execution.assemble import Page
from src.execution.federation import FederationResult, SourceFetch
from src.models.errors import ApiError
from src.observability.metrics import RATE_LIMIT_REMAINING_NAME
from tests.unit.test_assemble import envelope, make_plan

# --- freshness (HLD §9) -----------------------------------------------------


async def test_freshness_reports_the_stalest_contributor(parser, assembler, fake_clock):
    """Not the average and not the newest. A caller comparing it to their
    tolerance is asking "is ALL of this fresh enough", which is the only safe
    question about a joined result."""
    plan = make_plan(parser)
    # Stamps derived from the SAME injected clock the assembler reads, in the
    # seconds unit `AdapterResponse.fetched_at` uses.
    now_s = fake_clock() / 1000.0
    old = now_s - 30
    new = now_s - 1
    fetches = tuple(
        SourceFetch(
            alias=alias,
            connector_type=plan.sources[alias].connector_type,
            plan=plan.sources[alias],
            elapsed_ms=1.0,
            response=AdapterResponse(rows=[], fetched_at=stamp, served="live"),
        )
        for alias, stamp in (("pr", new), ("issue", old))
    )
    result = FederationResult(rows=[], columns=(), fetches=fetches)
    env = await assembler.assemble(plan, result, "tenant_acme", "t", 60_000, Page(0, 50))
    assert 29_000 < env.freshness_ms < 31_000  # the OLD one, not the new one


async def test_freshness_is_none_when_nothing_was_fetched(parser, assembler, fake_clock):
    """Honest: there is no data whose age could be reported. Zero would claim
    perfect freshness for an answer with no contributors."""
    plan = make_plan(parser)
    result = FederationResult(rows=[], columns=(), fetches=())
    env = await assembler.assemble(plan, result, "tenant_acme", "t", 60_000, Page(0, 50))
    assert env.freshness_ms is None


# --- GATE: the three outcomes stay distinct ---------------------------------


async def test_a_successful_query_is_complete_and_not_partial(parser, adapters, assembler):
    env = await envelope(parser, adapters, assembler)
    assert len(env.rows) == 3
    assert env.partial is False
    assert env.join_status == "complete"
    assert env.warnings == []


async def test_empty_is_a_correct_answer_not_a_degraded_one(parser, adapters, assembler):
    """**The `empty` leg.** carol ran fine and matched nothing."""
    env = await envelope(parser, adapters, assembler, user_id="carol")
    assert env.rows == []
    assert env.partial is False
    assert env.join_status == "complete"
    assert env.warnings == []


async def test_partial_is_visibly_different_from_empty(parser, adapters, assembler):
    """**The `partial` leg.** Rows present, a side missing, and said so."""
    adapters[("jira", "issues")].fail_next(FailureMode.TIMEOUT)
    env = await envelope(parser, adapters, assembler)
    assert env.partial is True
    assert env.join_status == "incomplete"
    assert [w["code"] for w in env.warnings] == ["SOURCE_TIMEOUT"]
    assert env.warnings[0]["connector"] == "jira"


async def test_a_timed_out_join_never_passes_the_unjoined_side_off_as_joined(
    parser, adapters, assembler
):
    """The sharpest honesty requirement in the phase file.

    GitHub returned 14 rows. If those were handed back as "the joined answer",
    a caller would receive PRs with no issue attached and no way to tell.
    """
    adapters[("jira", "issues")].fail_next(FailureMode.TIMEOUT)
    env = await envelope(parser, adapters, assembler)
    assert len(env.rows) < 14
    assert env.join_status == "incomplete"
    github = [s for s in env.sources if s.connector == "github"][0]
    assert github.state == "ok"


async def test_state_and_served_are_reported_separately(parser, adapters, assembler):
    """`state` is how the fetch ended; `served` is where the rows came from — so
    a timeout still answered from cache is distinguishable from one that
    returned nothing."""
    env = await envelope(parser, adapters, assembler)
    assert {s.served for s in env.sources} == {"live"}
    assert {s.state for s in env.sources} == {"ok"}


async def test_error_is_the_third_shape(parser, adapters, assembler):
    """**The `error` leg.** No envelope at all — it raises before assembly."""
    adapters[("jira", "issues")].fail_next(FailureMode.AUTH)
    with pytest.raises(ApiError):
        await envelope(parser, adapters, assembler)


# --- join_status ------------------------------------------------------------


async def test_a_query_with_no_join_reports_na(parser, adapters, assembler):
    """`n/a` is not a hedge: "was the join complete" has no answer for a query
    that did not join."""
    env = await envelope(
        parser,
        adapters,
        assembler,
        sql="SELECT issue.key, issue.status FROM jira.issues issue",
        policies=(),
    )
    assert env.join_status == "n/a"


# --- budgets ----------------------------------------------------------------


async def test_rate_limit_status_is_reported_per_connector(parser, adapters, assembler):
    env = await envelope(parser, adapters, assembler)
    assert set(env.rate_limit_status) == {"github", "jira"}
    assert env.rate_limit_status["github"].throttled is False
    assert env.rate_limit_status["github"].remaining > 0


async def test_reading_the_budget_does_not_spend_a_token(parser, adapters, assembler):
    """`remaining()` uses the same Lua as `consume()` with amount=0, so the
    refill arithmetic has exactly one implementation — and reporting the budget
    cannot cost one."""
    env = await envelope(parser, adapters, assembler)
    before = env.rate_limit_status["github"].remaining
    again = await envelope(parser, adapters, assembler)
    # The second query is served from cache, so no token is spent either.
    assert again.rate_limit_status["github"].remaining >= before


# --- stats ------------------------------------------------------------------


async def test_connector_ms_covers_only_live_calls(parser, adapters, assembler):
    """A cache hit costing 0.4ms reported as connector time would make the
    Phase 4 trace claim a source was fast when it was never called. The HLD's
    own envelope example omits Jira for exactly this reason."""
    first = await envelope(parser, adapters, assembler)
    assert set(first.stats["connector_ms"]) == {"github", "jira"}

    cached = await envelope(parser, adapters, assembler)
    assert cached.stats["connector_ms"] == {}
    assert {s.served for s in cached.sources} == {"cache"}


async def test_stage_timings_are_carried_through(parser, adapters, assembler):
    plan = make_plan(parser)
    result = FederationResult(rows=[], columns=(), fetches=())
    env = await assembler.assemble(
        plan, result, "tenant_acme", "t", 60_000, Page(0, 50), stats={"parse_ms": 1.5}
    )
    assert env.stats["parse_ms"] == 1.5


async def test_the_trace_id_is_carried_verbatim(parser, adapters, assembler):
    env = await envelope(parser, adapters, assembler)
    assert env.trace_id == "trace-abc"


# --- STALE_DATA -------------------------------------------------------------


async def test_a_stale_answer_warns_but_still_returns_rows(parser, assembler, fake_clock):
    """STALE_DATA rides back on a 200 (design-doc §8.1): the answer is usable
    but caveated, and failing the query would throw away good rows."""
    plan = make_plan(parser)
    fetches = (
        SourceFetch(
            alias="issue",
            connector_type="jira",
            plan=plan.sources["issue"],
            elapsed_ms=1.0,
            response=AdapterResponse(
                rows=[], fetched_at=(fake_clock() / 1000.0) - 600, served="cache"
            ),
        ),
    )
    result = FederationResult(rows=[["a"]], columns=(("k", "string"),), fetches=fetches)
    env = await assembler.assemble(plan, result, "tenant_acme", "t", 1000, Page(0, 50))
    assert [w["code"] for w in env.warnings] == ["STALE_DATA"]
    assert env.rows == [["a"]]
    assert env.partial is False


async def test_the_gauge_publishes_exactly_what_the_envelope_reports(parser, adapters, assembler):
    """ADR-043, and the reason the gauge is fed from here rather than anywhere else.

    Two separate reads of the bucket could disagree — the caller's banner would
    say one number and `/metrics` another, and neither would be wrong enough to
    notice. One read feeding both makes that impossible, so the assertion is
    equality against the envelope rather than "the gauge has some value".
    """
    env = await envelope(parser, adapters, assembler)

    for connector, budget in env.rate_limit_status.items():
        published = REGISTRY.get_sample_value(
            RATE_LIMIT_REMAINING_NAME,
            {"connector": connector, "tenant": "tenant_acme"},
        )
        assert published == budget.remaining


async def test_the_gauge_is_labelled_for_every_connector_the_query_touched(
    parser, adapters, assembler
):
    """Including one served from cache — "we did not need your budget" is part
    of the answer, and a missing series reads as a missing connector."""
    await envelope(parser, adapters, assembler)

    for connector in ("github", "jira"):
        assert (
            REGISTRY.get_sample_value(
                RATE_LIMIT_REMAINING_NAME,
                {"connector": connector, "tenant": "tenant_acme"},
            )
            is not None
        )
