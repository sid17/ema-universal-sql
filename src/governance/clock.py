"""The time source for state shared through Redis.

**Wall-clock epoch milliseconds, not monotonic.** ``time.monotonic()`` is only
meaningful within one process's lifetime, so two app replicas comparing
monotonic timestamps against the *same* Redis bucket would disagree about how
much time had passed and silently over- or under-admit requests. Anything whose
state outlives the process must use wall time. (``ControlPlaneRepository`` uses
``time.monotonic()`` and is right to: its cache is in-process and never shared.)

**The clock is injected, not called directly.** ``test_bucket_burst`` asserts
that after exactly one refill interval
exactly one more request is admitted. With a hard-coded clock that assertion
costs a real 12-second sleep inside the suite the pre-commit hook runs; with an
injected clock it is an in-memory comparison. The pattern is taken from
``PyrateLimiter``'s ``AbstractClock``.
"""

import time
from collections.abc import Callable

#: A zero-argument callable returning epoch milliseconds. Injected so tests can
#: advance time without sleeping; production always gets :func:`wall_clock_ms`.
NowMs = Callable[[], int]


def wall_clock_ms() -> int:
    """Epoch milliseconds from the wall clock. The production default."""
    return time.time_ns() // 1_000_000


class FakeClock:
    """A clock that only moves when a test moves it.

    Lives in ``src/`` rather than ``tests/`` because ``NowMs`` is part of the
    public constructor signature of everything that takes a clock — a caller
    reading :class:`FakeClock` alongside :func:`wall_clock_ms` can see both
    shapes of the contract in one place. The *fixture* that wires it into tests
    lives in ``tests/unit/conftest.py``, so the convenient path is the correct one.
    """

    #: An arbitrary fixed epoch-ms instant, so a failure message shows a stable
    #: number rather than whenever the suite happened to run.
    DEFAULT_START_MS = 1_700_000_000_000

    def __init__(self, start_ms: int = DEFAULT_START_MS) -> None:
        self._now_ms = int(start_ms)

    def __call__(self) -> int:
        """Satisfy :data:`NowMs` — the clock *is* the callable."""
        return self._now_ms

    def advance(self, ms: int) -> int:
        """Move time forward. Returns the new instant.

        Refuses to go backwards: a negative advance would let a test construct a
        state the wall clock cannot produce, so a bucket bug that only appears
        under time travel would look like a passing test.
        """
        if ms < 0:
            raise ValueError(f"time does not run backwards (got {ms}ms)")
        self._now_ms += int(ms)
        return self._now_ms
