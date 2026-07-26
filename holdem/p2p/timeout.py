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
    """Minimal clock surface: just what Session needs to decide when to
    broadcast a timeout proposal.  Wall time is NOT used to apply or
    reject proposals — that job belongs to the action sequence number."""

    def monotonic(self) -> float: ...


class RealClock:
    """Default production clock; wraps time.monotonic."""

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    """Deterministic clock for tests.  Call advance() to move time forward."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def monotonic(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


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
