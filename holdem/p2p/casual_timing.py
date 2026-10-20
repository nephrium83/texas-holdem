"""Local scheduling for the approved casual table clocks.

These helpers never change poker state or authorize a remote timeout. A
clock decides when the local game should send its normal action or the
elected timekeeper's signed notice; Session decides what those messages do.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from holdem.p2p.timeout import Clock


TURN_SECONDS = 30.0
TURN_DROP_SECONDS = 45.0
AUTOMATIC_SECONDS = 30.0
NEXT_HAND_SECONDS = 30.0
CLOCK_JUMP_SECONDS = 30.0
PREGAME_CLOCK_SKEW_SECONDS = 20.0


@dataclass(frozen=True)
class WaitToken:
    """One public contribution expected at a particular protocol position.

    ``detail`` distinguishes such steps as shuffle rounds and board
    streets. It must contain public identifiers, never cards or key shares.
    Each actor gets a separate token so another player's progress cannot
    restart this actor's clock.
    """

    hand: int
    phase: str
    actor: int
    action_seq: int
    detail: str = ""


class WaitClock:
    """Track monotonic start times while preserving unchanged waits."""

    def __init__(self, clock: Clock):
        self._clock = clock
        self._started: dict[WaitToken, float] = {}

    def sync(self, waits: Iterable[WaitToken]) -> None:
        now = self._clock.monotonic()
        self._started = {token: self._started.get(token, now)
                         for token in dict.fromkeys(waits)}

    @property
    def tokens(self) -> tuple[WaitToken, ...]:
        return tuple(self._started)

    def elapsed(self, token: WaitToken) -> float:
        return max(0.0, self._clock.monotonic() - self._started[token])

    def remaining(self, token: WaitToken, seconds: float) -> float:
        return max(0.0, seconds - self.elapsed(token))

    def expired(self, token: WaitToken, seconds: float) -> bool:
        return self.elapsed(token) >= seconds

    def clear(self) -> None:
        self._started.clear()


class ClockJumpGuard:
    """Detect and remember a wall-clock jump against monotonic elapsed time.

    Honest fixed clock drift is checked separately before play. Updating
    the baseline on every sample would hide a sequence of smaller changes,
    so the origin remains fixed for the lifetime of this table.
    """

    def __init__(self, clock: Clock):
        self._clock = clock
        self._wall = clock.time()
        self._mono = clock.monotonic()
        self._jumped = False

    def jumped(self) -> bool:
        difference = ((self._clock.time() - self._wall)
                      - (self._clock.monotonic() - self._mono))
        self._jumped = self._jumped or abs(difference) > CLOCK_JUMP_SECONDS
        return self._jumped
