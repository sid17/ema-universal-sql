"""The forced-failure hook, and the bug that moved it into Redis.

The hook used to be a dict on `ConnectorRegistry`, which is per *process* — and
the image runs eight uvicorn workers. `scripts/demo.sh` uses a separate `curl`
per call, so the arm and the query landed on different workers and the hook
fired about one time in eight: the committed demo artifact said `partial: true`
while a fresh run said `partial: false`.

`test_two_registries_share_one_armed_hook` is the regression test. It builds
TWO registries over ONE Redis — which is exactly what two workers are — and is
the assertion the old design could not pass.
"""

import fakeredis.aioredis
import pytest

from src.connectors.errors import FailureMode
from src.governance.failure_hooks import TTL_MS, FailureHookStore
from src.models.errors import InvalidQueryError
from src.pipeline.registry import ConnectorRegistry


@pytest.fixture
def store(fake_redis) -> FailureHookStore:
    return FailureHookStore(fake_redis)


def registry_for(control_plane, cache, limiter, secrets, redis) -> ConnectorRegistry:
    """One worker: its own registry object, the shared Redis."""
    return ConnectorRegistry(
        control_plane, cache, limiter, secrets, failures=FailureHookStore(redis)
    )


# --- the store --------------------------------------------------------------


async def test_an_armed_hook_comes_back(store) -> None:
    await store.arm("jira", FailureMode.TIMEOUT)

    assert await store.take("jira") is FailureMode.TIMEOUT


async def test_nothing_armed_is_not_a_failure(store) -> None:
    """The common case, on every request. It must be a `None`, not an error."""
    assert await store.take("jira") is None


async def test_the_hook_is_one_shot(store) -> None:
    """The request AFTER a forced failure must behave normally — that is what
    lets a demo show the recovery as well as the failure."""
    await store.arm("github", FailureMode.AUTH)

    assert await store.take("github") is FailureMode.AUTH
    assert await store.take("github") is None


async def test_arming_one_connector_leaves_the_other_alone(store) -> None:
    await store.arm("jira", FailureMode.TIMEOUT)

    assert await store.take("github") is None
    assert await store.take("jira") is FailureMode.TIMEOUT


async def test_take_all_consumes_every_armed_hook(store) -> None:
    await store.arm("jira", FailureMode.TIMEOUT)
    await store.arm("github", FailureMode.THROTTLED)

    taken = await store.take_all(["github", "jira"])

    assert taken == {"github": FailureMode.THROTTLED, "jira": FailureMode.TIMEOUT}
    assert await store.take_all(["github", "jira"]) == {}


async def test_an_armed_hook_expires(store, fake_redis) -> None:
    """A hook left behind by a crashed run must not sabotage an unrelated one
    minutes later."""
    await store.arm("jira", FailureMode.TIMEOUT)

    ttl = await fake_redis.pttl(store.key("jira"))

    assert 0 < ttl <= TTL_MS


async def test_an_unreadable_hook_raises_rather_than_serving_normally(store, fake_redis) -> None:
    """LAW 4. Swallowing it would leave the caller wondering why nothing failed."""
    await fake_redis.set(store.key("jira"), "not-a-failure-mode")

    with pytest.raises(ValueError):
        await store.take("jira")


# --- the regression: two workers, one hook ---------------------------------


async def test_two_registries_share_one_armed_hook(
    control_plane, cache, limiter, secrets, fake_redis
) -> None:
    """**The bug this module exists for.**

    Two `ConnectorRegistry` objects over one Redis is what two uvicorn workers
    are. The hook is armed through the first and must fire on the second. With
    the old per-process dict this returned a healthy adapter and `make demo`
    quietly reported `partial: false`.
    """
    arming_worker = registry_for(control_plane, cache, limiter, secrets, fake_redis)
    serving_worker = registry_for(control_plane, cache, limiter, secrets, fake_redis)

    await arming_worker.fail_next("jira", FailureMode.TIMEOUT)
    adapters = await serving_worker.adapters()

    assert adapters[("jira", "issues")]._forced_failure is FailureMode.TIMEOUT
    assert adapters[("github", "pull_requests")]._forced_failure is None


async def test_the_hook_is_consumed_by_whichever_worker_serves_first(
    control_plane, cache, limiter, secrets, fake_redis
) -> None:
    """One-shot across workers too, not just within one."""
    first = registry_for(control_plane, cache, limiter, secrets, fake_redis)
    second = registry_for(control_plane, cache, limiter, secrets, fake_redis)

    await first.fail_next("jira", FailureMode.TIMEOUT)
    await first.adapters()
    later = await second.adapters()

    assert later[("jira", "issues")]._forced_failure is None


async def test_an_independent_redis_does_not_see_the_hook(
    control_plane, cache, limiter, secrets, fake_redis
) -> None:
    """The scope is the DEPLOYMENT, not the globe: two stacks sharing this code
    but not this Redis must not arm each other's failures."""
    other_redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        armed = registry_for(control_plane, cache, limiter, secrets, fake_redis)
        elsewhere = registry_for(control_plane, cache, limiter, secrets, other_redis)

        await armed.fail_next("jira", FailureMode.TIMEOUT)

        assert (await elsewhere.adapters())[("jira", "issues")]._forced_failure is None
    finally:
        await other_redis.aclose()


# --- refusals ---------------------------------------------------------------


async def test_an_unknown_connector_is_refused_rather_than_stored(
    control_plane, cache, limiter, secrets, fake_redis
) -> None:
    """Otherwise a caller could write keys that only expire on their TTL."""
    registry = registry_for(control_plane, cache, limiter, secrets, fake_redis)

    with pytest.raises(InvalidQueryError, match="unknown connector"):
        await registry.fail_next("slack", FailureMode.TIMEOUT)

    assert await fake_redis.get(FailureHookStore.key("slack")) is None


async def test_arming_without_a_store_is_a_loud_wiring_bug(
    control_plane, cache, limiter, secrets
) -> None:
    """`main.py` builds the store only in TEST_MODE. Reaching this call without
    one means the route's guard and the wiring disagree."""
    registry = ConnectorRegistry(control_plane, cache, limiter, secrets)

    with pytest.raises(RuntimeError, match="no forced-failure store"):
        await registry.fail_next("jira", FailureMode.TIMEOUT)


async def test_a_registry_with_no_store_still_builds_adapters(
    control_plane, cache, limiter, secrets
) -> None:
    """The production path: no store, no Redis round trip, no hooks."""
    registry = ConnectorRegistry(control_plane, cache, limiter, secrets)

    adapters = await registry.adapters()

    assert set(adapters) == {("github", "pull_requests"), ("jira", "issues")}
    assert all(adapter._forced_failure is None for adapter in adapters.values())
