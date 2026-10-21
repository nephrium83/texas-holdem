"""Presentation pacing for the desktop's in-process opponents.

The engine and wire protocol still decide whose turn it is. This adapter only
waits before sending a bot's normal action. Its owner calls ``refresh`` after
the outer bus drain, so no bot can act inside Start game's synchronous deal.
The source sidecar's immediate BotDriver remains available to embedders.
"""
from __future__ import annotations

import logging
import threading
import time

from holdem import client_view
from holdem.engine import Brain

_log = logging.getLogger(__name__)

# Godot's shuffle/deal lasts at most 2.3s. Keep the opening action behind it
# even when a bot is first to act. Every later bot decision remains visible.
HAND_PRESENTATION_SECONDS = 2.8
BOT_TURN_SECONDS = 0.65


def _schedule_timer(delay, callback):
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.start()
    return timer


class PacedBotDriver:
    """One cancellable timer, serialized with desktop commands and snapshots.

    ``schedule(delay, callback)`` returns an object with ``cancel()``. Both it
    and the monotonic ``clock`` can be supplied by deterministic tests. The
    owner must hold ``lock`` when calling refresh/close; timer callbacks take
    that same lock before examining or changing any session or bus state.
    """

    def __init__(self, sessions, bus, rng, *, lock, on_change, on_error,
                 clock=None, schedule=None):
        self._sessions = sessions
        self._bus = bus
        self._brain = Brain(rng)
        self._lock = lock
        self._on_change = on_change
        self._on_error = on_error
        self._clock = clock or time.monotonic
        self._schedule = schedule or _schedule_timer
        self._pending = {}
        self._hand_ready = {}
        self._timer = None
        self._armed_due = None
        self._generation = 0
        self._closed = False

    def _candidate(self, conn_id, session, now):
        snap = client_view.snapshot(session)
        if (session.terminal_state is not None or snap.get("session_over")
                or snap.get("eliminated")):
            return None
        hand = snap["hand_num"]
        if snap["phase"] == "betting":
            self._hand_ready.setdefault(hand, now + HAND_PRESENTATION_SECONDS)
        state = snap.get("turn", {}).get("state")
        if state in ("hand_complete", "voided"):
            # Readiness is not a betting action: applying the new-hand grace
            # here would leave peers waiting for a deal that cannot start.
            return (conn_id, "next", hand), now + BOT_TURN_SECONDS
        if "legal" in snap.get("you", {}) and session.replica is not None:
            return ((conn_id, "bet", hand, session.replica.next_seq),
                    max(now + BOT_TURN_SECONDS, self._hand_ready[hand]))
        return None

    def refresh(self):
        """Observe a quiescent bus; keep deadlines for unchanged legal work."""
        if self._closed:
            return
        now = self._clock()
        wanted = {}
        for conn_id, session in self._sessions.items():
            candidate = self._candidate(conn_id, session, now)
            if candidate is not None:
                key, due = candidate
                wanted[key] = self._pending.get(key, due)
        self._pending = wanted
        observed_hands = {session.replica.hand_no
                          for session in self._sessions.values()
                          if session.replica is not None}
        self._hand_ready = {hand: due for hand, due in self._hand_ready.items()
                            if hand in observed_hands}
        self._arm()

    def _arm(self):
        due = min(self._pending.values(), default=None)
        if due == self._armed_due:
            return
        self._generation += 1
        if self._timer is not None:
            self._timer.cancel()
        self._timer = None
        self._armed_due = due
        if due is not None:
            generation = self._generation
            self._timer = self._schedule(
                max(0.0, due - self._clock()),
                lambda: self._wake(generation),
            )

    def _wake(self, generation):
        with self._lock:
            # cancel() can lose a race with an already-started callback. The
            # generation check also rejects those callbacks after leave or
            # when a command invalidated a scheduled hand/action identity.
            if self._closed or generation != self._generation:
                return
            try:
                self._run_due()
            except Exception:
                _log.exception("desktop bot action failed")
                # A failed delivery can already have advanced some replicas.
                # Retrying the same action, or offering a new hand, would hide
                # that uncertainty. The owner closes this table visibly and
                # leaves the last agreed balances available for inspection.
                self.close()
                self._on_error()

    def _run_due(self):
        self._timer = None
        self._armed_due = None
        now = self._clock()
        key = min(self._pending, key=self._pending.get, default=None)
        if key is None or self._pending[key] > now:
            self._arm()
            return
        del self._pending[key]
        session = self._sessions[key[0]]
        candidate = self._candidate(key[0], session, now)
        if candidate is None or candidate[0] != key:
            self.refresh()
            return
        if key[1] == "next":
            verdict = session.next_p2p_hand()
            if verdict not in ("started", "not_ready", "session_over", "eliminated"):
                raise RuntimeError(f"unexpected bot Next Hand verdict: {verdict!r}")
        else:
            action, amount = self._brain.decide(
                session.replica.engine, session.local_seat)
            verdict = session.send_bet_action(action, amount)
            if verdict != "applied":
                raise RuntimeError(f"bot action was refused: {verdict!r}")
        # Timer callbacks are outer operations, never Session hooks.
        # Delivery finishes before observing another legal action.
        self._bus.drain()
        self.refresh()
        self._on_change()

    def close(self):
        """Invalidate even callbacks that have already escaped cancel()."""
        self._closed = True
        self._generation += 1
        if self._timer is not None:
            self._timer.cancel()
        self._timer = None
        self._armed_due = None
        self._pending.clear()
        self._hand_ready.clear()

