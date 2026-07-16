"""Per-seat session statistics for the Texas Hold'em HUD.

SessionStats tracks VPIP, PFR, hands dealt and hands won for every seat
across the session.  It intentionally carries no tkinter dependency so it
can be unit-tested headlessly.

Definitions
-----------
VPIP  – Voluntarily Put (money) In Pot (pre-flop).  A seat counts as VPIP
        for a hand when it makes a *voluntary* call or raise pre-flop.
        Posting the big blind and then checking one's option is NOT VPIP.
        Posting the small blind (which is forced) is NOT VPIP.
PFR   – Pre-Flop Raise.  Any pre-flop raise or re-raise by the seat.
"""
from __future__ import annotations


class SessionStats:
    """Accumulate per-seat VPIP / PFR / hands stats across a session."""

    def __init__(self) -> None:
        # seat_idx -> {hands_dealt, vpip_count, pfr_count, hands_won}
        self._data: dict[int, dict[str, int]] = {}

    # ------------------------------------------------------------------ API

    def record_hand_start(self, players) -> None:
        """Call with the engine's player list when a new hand is dealt."""
        for p in players:
            if p.in_seat:
                d = self._ensure(p.idx)
                d["hands_dealt"] += 1

    def record_voluntary_action(self, seat_idx: int) -> None:
        """Count a pre-flop voluntary money-in (call or raise, not BB check)."""
        d = self._ensure(seat_idx)
        # Guard against double-counting within one hand:
        # We only count once per hand; callers are responsible for calling
        # this at most once per hand per seat.
        d["vpip_count"] += 1

    def record_pfr(self, seat_idx: int) -> None:
        """Count a pre-flop raise (bet / raise / re-raise) by seat_idx."""
        d = self._ensure(seat_idx)
        d["pfr_count"] += 1

    def record_win(self, seat_idx: int) -> None:
        """Count a pot win for seat_idx."""
        d = self._ensure(seat_idx)
        d["hands_won"] += 1

    def hands_dealt(self, seat_idx: int) -> int:
        return self._data.get(seat_idx, {}).get("hands_dealt", 0)

    def vpip_pct(self, seat_idx: int) -> float:
        d = self._data.get(seat_idx, {})
        h = d.get("hands_dealt", 0)
        return d.get("vpip_count", 0) / h * 100.0 if h else 0.0

    def pfr_pct(self, seat_idx: int) -> float:
        d = self._data.get(seat_idx, {})
        h = d.get("hands_dealt", 0)
        return d.get("pfr_count", 0) / h * 100.0 if h else 0.0

    def hud_line(self, seat_idx: int) -> str:
        """Return the one-line HUD string for a seat, or '' if too few hands."""
        h = self.hands_dealt(seat_idx)
        if h < 3:
            return ""
        vpip = self.vpip_pct(seat_idx)
        return f"{vpip:.0f}%  ·  {h}h"

    # ---------------------------------------------------------------- helpers

    def _ensure(self, seat_idx: int) -> dict[str, int]:
        if seat_idx not in self._data:
            self._data[seat_idx] = {
                "hands_dealt": 0,
                "vpip_count": 0,
                "pfr_count": 0,
                "hands_won": 0,
            }
        return self._data[seat_idx]
