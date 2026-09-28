"""The injected clock: deterministic in tests, wall-clock in production."""

import math
import time

import pytest

from src.governance.clock import FakeClock, wall_clock_ms


def test_wall_clock_returns_epoch_milliseconds():
    before = time.time()
    value = wall_clock_ms()
    after = time.time()
    # Milliseconds, not seconds and not nanoseconds: the value has to sit
    # between two wall-clock readings once both are converted. `before` is
    # floored because wall_clock_ms truncates sub-millisecond precision, so an
    # un-floored float bound is off by a fraction of a millisecond.
    assert math.floor(before * 1000) <= value <= math.ceil(after * 1000)


def test_wall_clock_is_not_monotonic():
    """The distinction ADR-019 turns on, asserted rather than assumed.

    A monotonic clock counts from an arbitrary origin (often process or boot
    start) and is typically a small number; epoch milliseconds are ~1.7e12.
    If this ever fails, bucket state shared through Redis has become wrong
    across replicas in a way no other test would catch.
    """
    assert wall_clock_ms() > time.monotonic() * 1000


def test_fake_clock_does_not_move_on_its_own():
    clock = FakeClock()
    first = clock()
    assert clock() == first
    assert clock() == first


def test_fake_clock_advances_by_exactly_what_it_is_given():
    clock = FakeClock(start_ms=1_000)
    assert clock() == 1_000
    assert clock.advance(12_000) == 13_000
    assert clock() == 13_000


def test_fake_clock_refuses_to_run_backwards():
    clock = FakeClock()
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)


def test_fake_clock_satisfies_the_now_ms_contract():
    """Whatever takes a ``NowMs`` must accept a FakeClock without adaptation."""

    def takes_a_clock(now_ms) -> int:
        return now_ms()

    assert takes_a_clock(FakeClock(start_ms=42)) == 42
    assert takes_a_clock(wall_clock_ms) > 0
