"""Local clocks schedule ordinary actions, independently of wall time."""
import pytest

from holdem.p2p.casual_timing import (
    AUTOMATIC_SECONDS, NEXT_HAND_SECONDS, TURN_DROP_SECONDS, TURN_SECONDS,
    ClockJumpGuard, WaitClock, WaitToken,
)
from holdem.p2p.timeout import FakeClock


def test_contracted_casual_intervals():
    assert (TURN_SECONDS, TURN_DROP_SECONDS,
            AUTOMATIC_SECONDS, NEXT_HAND_SECONDS) == (30, 45, 30, 30)


def test_other_players_progress_does_not_restart_silent_actor():
    clock = FakeClock()
    waits = WaitClock(clock)
    silent = WaitToken(1, "keygen", 2, 0)
    other = WaitToken(1, "keygen", 1, 0)
    waits.sync([silent, other])
    clock.advance(20)
    later = WaitToken(1, "shuffle", 0, 0, "1")
    waits.sync([silent, later])
    clock.advance(10)
    assert waits.expired(silent, AUTOMATIC_SECONDS)
    assert waits.remaining(later, AUTOMATIC_SECONDS) == 20
    assert other not in waits.tokens


@pytest.mark.parametrize("replacement", [
    WaitToken(2, "betting", 1, 0),
    WaitToken(1, "betting", 2, 0),
    WaitToken(1, "betting", 1, 1),
    WaitToken(1, "shuffle", 1, 0, "2"),
])
def test_new_protocol_position_gets_its_own_clock(replacement):
    clock = FakeClock()
    waits = WaitClock(clock)
    first = WaitToken(1, "betting", 1, 0)
    waits.sync([first])
    clock.advance(44)
    waits.sync([replacement])
    assert waits.elapsed(replacement) == 0


def test_same_turn_has_local_action_at_30_and_timekeeper_drop_at_45():
    clock = FakeClock()
    waits = WaitClock(clock)
    token = WaitToken(1, "betting", 1, 7)
    waits.sync([token, token])
    clock.advance(29.999)
    assert not waits.expired(token, TURN_SECONDS)
    clock.advance(.001)
    assert waits.expired(token, TURN_SECONDS)
    assert not waits.expired(token, TURN_DROP_SECONDS)
    clock.advance(15)
    assert waits.expired(token, TURN_DROP_SECONDS)
    assert waits.remaining(token, TURN_SECONDS) == 0
    assert waits.tokens == (token,)
    waits.clear()
    assert waits.tokens == ()


def test_wall_adjustment_does_not_expire_monotonic_wait():
    clock = FakeClock()
    waits = WaitClock(clock)
    token = WaitToken(1, "betting", 1, 0)
    waits.sync([token])
    clock.advance_wall(10_000)
    assert waits.elapsed(token) == 0
    assert not waits.expired(token, TURN_SECONDS)


@pytest.mark.parametrize("jump", [30.001, -30.001])
def test_clock_jump_is_detected_in_both_directions_and_remembered(jump):
    clock = FakeClock(wall_start=1000)
    guard = ClockJumpGuard(clock)
    clock.advance(100_000)
    assert not guard.jumped()
    clock.advance_wall(jump)
    assert guard.jumped()
    clock.advance_wall(-jump)
    assert guard.jumped()


def test_thirty_seconds_exact_is_permitted_but_small_jumps_accumulate():
    clock = FakeClock()
    guard = ClockJumpGuard(clock)
    for _ in range(3):
        clock.advance_wall(10)
        assert not guard.jumped()
    clock.advance_wall(.001)
    assert guard.jumped()
