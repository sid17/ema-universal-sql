"""The forced-failure hook, held in Redis rather than in one worker's memory.

``POST /v1/test/fail-next`` arms a one-shot connector failure so that "a source
times out, the answer degrades to partial" (brief line 84, DoD §2 hard part 5)
is demonstrable on demand instead of only during a real outage.

**Why Redis.** The hook used to live in a dict on ``ConnectorRegistry``, which is
per *process* — and the image runs ``uvicorn --workers 8``. ``scripts/demo.sh``
uses a separate ``curl`` per call, so each call opens a new TCP connection and
lands on whichever worker accepts it: the arm went to one worker and the query
to another, and the hook fired roughly one time in eight. The committed demo
artifact said ``partial: true`` while a fresh run said ``partial: false``.

The integration suite never caught it because ``httpx`` keeps one connection
alive and therefore sticks to one worker — ``test_timeout_partial.py`` passed
6/6 under the same eight workers that broke the demo. A bug only the artifact
could see is the worst kind to leave in.

**Off unless ``TEST_MODE``.** The store is constructed only in test mode
(``src/main.py``), so a production run holds ``None`` and pays nothing — the
hook is not merely refused by the route, it is structurally absent from the
fetch path.
"""

import logging

from src.connectors.errors import FailureMode

logger = logging.getLogger(__name__)

KEY_PREFIX = "failnext"

#: How long an armed hook survives unconsumed. Long enough for the next request
#: in a demo or a test, short enough that a hook armed by a crashed run cannot
#: sabotage an unrelated one minutes later.
TTL_MS = 60_000


class FailureHookStore:
    """One-shot forced failures, shared across every worker in the deployment."""

    def __init__(self, redis) -> None:
        self._redis = redis

    @staticmethod
    def key(connector_type: str) -> str:
        return f"{KEY_PREFIX}:{connector_type}"

    async def arm(self, connector_type: str, mode: FailureMode) -> None:
        """Make the next fetch of ``connector_type`` fail, whichever worker serves it."""
        await self._redis.set(self.key(connector_type), mode.value, px=TTL_MS)

    async def take(self, connector_type: str) -> FailureMode | None:
        """Consume the hook if one is armed.

        ``GETDEL`` rather than ``GET`` then ``DEL``: the read and the delete must
        be one operation, or two workers racing on the same armed hook would both
        fail their fetch and the "one-shot" guarantee — which is what lets a demo
        show the *recovery* as well as the failure — would not hold.
        """
        raw = await self._redis.getdel(self.key(connector_type))
        if raw is None:
            return None
        value = raw.decode() if isinstance(raw, bytes | bytearray) else str(raw)
        try:
            return FailureMode(value)
        except ValueError:
            # LAW 4: a hook we cannot read is a bug, not a reason to serve
            # normally and leave the caller wondering why nothing failed.
            logger.error(
                "discarding an unreadable forced-failure hook",
                extra={"context": {"connector": connector_type, "value": value}},
            )
            raise

    async def take_all(self, connector_types) -> dict[str, FailureMode]:
        """Consume every armed hook among ``connector_types``."""
        armed = {}
        for connector_type in connector_types:
            mode = await self.take(connector_type)
            if mode is not None:
                armed[connector_type] = mode
        return armed
