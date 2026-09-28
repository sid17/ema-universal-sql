"""The source's budget and the envelope's budget are the same number.

The connector renders `X-RateLimit-Remaining` from the `RateLimitDecision` the
token bucket returns; `ResultAssembler` fills `rate_limit_status` from the same
limiter. There is one arithmetic, in Redis — but "there is one arithmetic" is a
claim about wiring, and wiring is exactly what drifts. These tests drain a real
bucket over HTTP and check the two readings against each other.

`tenant_acme` is the tenant to drain: its GitHub budget is 5 requests per 60s
with a burst of 2, sized in `config/rate_limits.yaml` so a short loop empties it
deterministically.
"""

import pytest

ACME_GITHUB_CAPACITY = 7  # max_requests 5 + burst 2

#: NOTE: `ConnectorBudget` publishes `remaining` and `throttled`, not the
#: ceiling — so a caller reading the envelope sees "4 left" without knowing "of
#: how many", while the source's own `X-RateLimit-Limit` header does carry it.
#: Adding `limit` to the envelope would change a published response contract
#: (HLD §4), so it is named here rather than changed under this phase.


@pytest.fixture
def drain(run_query):
    """Spend GitHub tokens until the tenant is refused, or fail loudly.

    `max_staleness_ms=0` on every call: a cache hit would return 200 without
    spending a token, and the loop would never terminate — which is the
    behaviour ADR-024 exists to guarantee, and the reason this fixture cannot
    just hammer the default.
    """

    def _drain(limit: int = 40):
        seen = []
        for _ in range(limit):
            response = run_query(max_staleness_ms=0)
            seen.append(response)
            if response.status_code == 429:
                return seen
        pytest.fail(
            f"tenant_acme's GitHub bucket did not empty in {limit} live queries; "
            f"has config/rate_limits.yaml changed?"
        )

    return _drain


def test_the_envelope_reports_the_budget_the_source_reported(envelope):
    """Every successful query carries both connectors' remaining count."""
    body = envelope(max_staleness_ms=0)

    assert set(body["rate_limit_status"]) == {"github", "jira"}
    github = body["rate_limit_status"]["github"]
    assert 0 <= github["remaining"] <= ACME_GITHUB_CAPACITY
    assert github["throttled"] is False


def test_the_reported_budget_falls_by_one_per_live_query(run_query):
    """Not merely present — *moving*, and moving by the right amount.

    A `remaining` that never changed would satisfy every other assertion in
    this file while telling a caller nothing at all.
    """
    first = run_query(max_staleness_ms=0).json()["rate_limit_status"]["github"]
    second = run_query(max_staleness_ms=0).json()["rate_limit_status"]["github"]

    assert second["remaining"] == first["remaining"] - 1


def test_a_cache_hit_spends_nothing(run_query):
    """ADR-024 over HTTP: the budget models the DOWNSTREAM call, and a hit makes
    none. This is the property the whole load profile rests on."""
    run_query(max_staleness_ms=0)
    before = run_query(max_staleness_ms=60_000).json()["rate_limit_status"]["github"]
    after = run_query(max_staleness_ms=60_000).json()["rate_limit_status"]["github"]

    assert after["remaining"] == before["remaining"]


def test_draining_the_bucket_ends_in_a_429_that_tells_the_caller_what_to_do(drain):
    """The refusal a caller actually receives, whatever dialect the source used.

    GitHub's own primary limit answers 403; the connector normalises it, so what
    arrives here is a 429 with the vocabulary this system publishes.
    """
    responses = drain()
    refused = responses[-1]
    body = refused.json()

    assert refused.status_code == 429
    assert body["error_code"] == "RATE_LIMIT_EXHAUSTED"
    assert refused.headers.get("Retry-After") is not None
    assert body["suggested_action"], "a 429 must name the way out (brief line 110)"


def test_the_last_success_reported_a_budget_consistent_with_the_refusal(drain):
    """The join between the two readings.

    The last 200 before the wall must report a remaining count small enough to
    explain the refusal that follows. If the envelope said 4 remaining and the
    very next call was refused, the two readings would be describing different
    buckets.
    """
    responses = drain()
    successes = [r for r in responses if r.status_code == 200]
    assert successes, "the bucket was already empty; the reset fixture did not run"

    last_good = successes[-1].json()["rate_limit_status"]["github"]

    assert last_good["remaining"] == 0, (
        "the final successful query should have taken the last token; "
        f"it reported {last_good['remaining']} remaining"
    )


def test_the_number_of_successes_matches_the_seeded_capacity(drain):
    """A cold bucket admits `max_requests + burst` before throttling — so the
    count of 200s is the capacity, read back through the whole stack."""
    responses = drain()

    assert len([r for r in responses if r.status_code == 200]) == ACME_GITHUB_CAPACITY


def test_jiras_budget_is_untouched_by_githubs_exhaustion(drain):
    """Buckets are per (tenant, connector). One source hitting its wall must not
    spend another's tokens — otherwise a noisy connector would throttle a quiet
    one for the same tenant."""
    responses = drain()
    last_good = [r for r in responses if r.status_code == 200][-1].json()

    assert last_good["rate_limit_status"]["jira"]["remaining"] > 0
