"""Timeout machinery for the hostless peer-to-peer session.

DeadlineToken     -- frozen identifier for one expected peer action.
Clock / RealClock -- injectable clock; tests use FakeClock.
FakeClock         -- controllable clock for deterministic tests.

No sleeping. No hoping the scheduler is in a good mood.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Protocol


# ---------------------------------------------------------------------------
# Clock protocol
# ---------------------------------------------------------------------------

class Clock(Protocol):
    """Monotonic time for deadlines and records, wall time for timestamps.

    Wall time is not used to apply or reject timeout proposals; that job
    belongs to the action sequence number.
    """

    def monotonic(self) -> float: ...

    def time(self) -> float: ...


class RealClock:
    """Host-supplied production clock using the system's time sources."""

    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()


class FakeClock:
    """Deterministic clock with independently adjustable wall time."""

    def __init__(self, start: float = 0.0, *,
                 wall_start: Optional[float] = None) -> None:
        self._t = start
        self._wall = start if wall_start is None else wall_start

    def monotonic(self) -> float:
        return self._t

    def time(self) -> float:
        return self._wall

    def advance(self, seconds: float) -> None:
        self._t += seconds
        self._wall += seconds

    def advance_wall(self, seconds: float) -> None:
        self._wall += seconds


# ---------------------------------------------------------------------------
# DeadlineToken
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeadlineToken:
    """Uniquely identifies one expected contribution in one phase of one hand.

    ``hand_id``    -- session._deal_session_id(); encodes hand + seat order.
    ``phase``      -- canonical phase string from TIMEOUT_SPEC.md:
                      "betting" | "deal_shuffle" | "deal_decrypt" | ...
    ``actor``      -- conn_id of the awaited peer, or None for multi-peer
                      phases (shuffle, decrypt) where any missing peer ends it.
    ``action_seq`` -- replica.next_seq at the moment the deadline was set;
                      proposals with a different seq are stale and dropped.
    """

    hand_id:    str
    phase:      str
    actor:      Optional[str]
    action_seq: int


# ---------------------------------------------------------------------------
# Default phase timeouts (seconds)
# ---------------------------------------------------------------------------

DEFAULT_PHASE_TIMEOUTS: dict[str, float] = {
    "betting":         30.0,
    "deal_shuffle":    30.0,
    "deal_decrypt":    30.0,
    "lobby_handshake": 60.0,
    "lobby_ready":    120.0,
    "settlement_ack":  10.0,
}
