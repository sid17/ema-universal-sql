"""Where the mocks' simulated latency may and may not be applied (ADR-038).

Split from `test_connectors.py` when that file crossed LAW 1's 400-line
decompose threshold — and the seam is a real one: every other test in that file
asserts *what* an adapter returns, while every test here asserts *what it
costs*, which is a different question with a different failure mode.

HLD line 39 lists "simulated pagination/latency/429" as what the mocks provide.
Pagination and the 429 shipped in Phase 1; latency did not, and Phase 4 is where
the omission bites — see the module docstring of `src/connectors/mock_adapter.py`.

**Placement is the entire decision**, so it is what these tests pin: the sleep
must sit below the cache check, below the token consume, and below request
validation. Above any of those and it would break the `max_staleness_ms` demo,
the cache-before-token ordering (ADR-024), or the k6 profile.
"""

import time

import pytest

from src.connectors.base import FetchRequest
from src.connectors.github import GitHubConnectorAdapter
from src.connectors.jira import JiraConnectorAdapter
from src.models.errors import ApiError
from tests.unit.conftest import GITHUB_CAPABILITIES
from tests.unit.test_connectors import ACME, SCOPE, gh_request


def slow_github(cache, limiter, secrets, control_plane, fake_clock):
    """A GitHub adapter with the latency scale turned on for this test only.

    Passed explicitly rather than by patching `MOCK_LATENCY_SCALE`, so the
    commit-hook suite never sleeps and these assertions do not depend on
    process-wide settings that another test might have cached.
    """
    return GitHubConnectorAdapter(
        capabilities=GITHUB_CAPABILITIES,
        cache=cache,
        limiter=limiter,
        secrets=secrets,
        control_plane=control_plane,
        now_ms=fake_clock,
        latency_scale=1.0,
    )


def test_the_two_adapters_declare_different_latencies() -> None:
    """The contrast is the point: two equal bars prove nothing on a waterfall,
    and Jira is the slow one because it is the source carrying RLS and CLS."""
    assert JiraConnectorAdapter.simulated_latency_ms > GitHubConnectorAdapter.simulated_latency_ms


def test_the_default_scale_is_off_so_the_commit_hook_stays_instant(github) -> None:
    """LAW 6 runs `tests/unit` on every commit; a sleeping suite is a tax on
    every commit forever. The default must be an exact no-op."""
    assert github._latency_s == 0


async def test_a_live_fetch_pays_the_latency(
    cache, limiter, secrets, control_plane, fake_clock
) -> None:
    adapter = slow_github(cache, limiter, secrets, control_plane, fake_clock)

    started = time.perf_counter()
    response = await adapter.fetch(gh_request(max_staleness_ms=0))
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert response.served == "live"
    assert elapsed_ms >= GitHubConnectorAdapter.simulated_latency_ms


async def test_a_cache_hit_does_not(
    cache, limiter, secrets, control_plane, fake_clock
) -> None:
    """**The assertion the whole placement decision rests on.**

    Latency above the cache check would break three things at once: the
    `max_staleness_ms` demo (a hit would cost what a live fetch costs), the
    reason the token is spent after the cache rather than before (ADR-024), and
    the k6 profile, which is configured so cache hits dominate precisely so it
    measures this engine instead of these sleeps.
    """
    adapter = slow_github(cache, limiter, secrets, control_plane, fake_clock)
    await adapter.fetch(gh_request(max_staleness_ms=0))

    started = time.perf_counter()
    response = await adapter.fetch(gh_request(max_staleness_ms=60_000))
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert response.served == "cache"
    assert elapsed_ms < GitHubConnectorAdapter.simulated_latency_ms / 2


async def test_a_refused_fetch_pays_nothing(
    cache, limiter, secrets, control_plane, fake_clock
) -> None:
    """A rejected request makes no downstream call, so it may not cost one.

    Charging latency for a validation failure would make every capability
    rejection look like a slow source in the histogram it feeds.
    """
    adapter = slow_github(cache, limiter, secrets, control_plane, fake_clock)

    started = time.perf_counter()
    with pytest.raises(ApiError):
        await adapter.fetch(FetchRequest(tenant_id=ACME, entitlement_scope=SCOPE))
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert elapsed_ms < GitHubConnectorAdapter.simulated_latency_ms / 2
