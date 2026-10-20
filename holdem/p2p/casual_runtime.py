"""Owner-driven casual LAN clocks. No threads, transport globals or UI decisions.

Deadlines identify public protocol work. A timeout is an authenticated drop
decision by the elected timekeeper; receivers never compare elapsed clocks.
"""
from __future__ import annotations

from dataclasses import asdict
import math

from holdem.p2p.casual_timing import ClockJumpGuard, WaitClock, WaitToken


class CasualRuntime:
    def __init__(self, session):
        self.clock = WaitClock(session._clock)
        self.jump = ClockJumpGuard(session._clock)
        self.last_view = None

    def waits(self, s):
        r = s._replica
        if (s.terminal_state is not None or s.state != "PLAYING" or r is None
                or s._session_over or s._p2p_spectator):
            return []
        if s.casual_transition_pending:
            continuation = s._casual_continuation
            scope = continuation.ack_scope()
            return [WaitToken(scope["hand"], "drop_ack", seat,
                              scope["epoch"], str(scope["resume_hand"]))
                    for seat in continuation.missing_ack_seats()]
        hand, seq = s._hand_no, r.next_seq
        waits = []
        if s.hand_voided or s.hand_result is not None:
            waits.append(WaitToken(hand, "next_hand", s.local_seat, seq))
            if not s.hand_voided:
                waits.extend(WaitToken(hand, "settlement", seat, seq)
                             for seat in sorted(s._ended_seats - s._ended_agreed)
                             if seat not in s.departed_seats)
            return waits
        deal = getattr(s._deal_driver, "deal", None)
        if deal is not None:
            waits.extend(WaitToken(hand, item["phase"], item["actor"], seq,
                                   item.get("detail", ""))
                         for item in deal.public_waits()
                         if item["actor"] not in s.departed_seats)
        # Crypto work precedes betting; a dummy replica actor isn't a turn.
        if not waits and s._own_hole_set and r.phase == "betting":
            actor = r.engine.actor
            if actor is not None and actor not in s.departed_seats:
                waits.append(WaitToken(hand, "action", actor, seq))
        return waits

    def sync(self, s):
        waits = self.waits(s)
        self.clock.sync(self._clock_token(token) for token in waits)
        return waits

    @staticmethod
    def _clock_token(token):
        # The live action count is included in a signed notice to locate
        # the drop precisely. It is not progress on a missing crypto
        # contribution: another player's bet must not restart that clock.
        if token.phase not in ("action", "drop_ack"):
            return WaitToken(token.hand, token.phase, token.actor, 0, token.detail)
        return token

    def view(self, s):
        self.sync(s)
        tokens = self.clock.tokens
        if not tokens or s.terminal_state is not None:
            return None
        # Show blocking protocol work ahead of the parallel Next countdown.
        work = [t for t in tokens if t.phase != "next_hand"] or list(tokens)
        t = min(work, key=lambda token: self.clock.remaining(token, 30.0))
        kind = (t.phase if t.phase in ("action", "next_hand") else "automatic")
        seats = sorted({v.actor for v in work})
        cfg = s._table_cfg or {}
        names = cfg.get("names", [])
        awaited = [names[v] if 0 <= v < len(names) else f"Player {v + 1}"
                   for v in seats]
        labels = {"action": "Turn", "next_hand": "Next hand",
                  "keygen": "Preparing deal", "shuffle": "Shuffling",
                  "holes": "Dealing cards", "board": "Revealing board",
                  "audit": "Verifying hand", "settlement": "Confirming result",
                  "drop_ack": "Confirming player departure"}
        label = labels.get(t.phase, "Waiting for players")
        seconds = 30.0
        if t.phase == "action" and self.clock.expired(t, 30.0):
            kind, label, seconds = "automatic", "Waiting for response", 45.0
        if kind != "next_hand":
            label += ": " + ", ".join(awaited)
        result = {"kind": kind, "label": label,
                  "remaining_seconds": math.ceil(self.clock.remaining(t, seconds)),
                  "waiting_for": awaited,
                  "timekeeper_seat": s.casual_timekeeper_seat()}
        if t.phase == "action" and kind == "action":
            result["timeout_action"] = (
                "check" if s._replica.engine.legal(t.actor)["can_check"] else "fold")
        return result

    def tick(self, s):
        if s.state != "PLAYING" or s.terminal_state is not None:
            return
        if self.jump.jumped():
            fault = {"type": "casual_clock_fault", "hand": s._hand_no,
                     "seat": s.local_seat}
            s._send_hostless(fault)
            clock_fault(s, fault)
            if s.terminal_state is None:
                s.terminate(s.PEER_LOST, "Your clock changed during play (clock)")
            return
        waits = self.sync(s)
        for token in waits:
            scheduled = self._clock_token(token)
            if token.phase == "action" and token.actor == s.local_seat:
                if self.clock.expired(scheduled, 30.0):
                    action = ("check" if s._replica.engine.legal(token.actor)["can_check"]
                              else "fold")
                    # The engine's normal "call" command checks when
                    # to_call is zero; "check" is a display/event label.
                    s.send_bet_action("call" if action == "check" else action)
                    s._safe_emit("casual_auto_action", hand=token.hand,
                                 seat=token.actor, action=action, seq=token.action_seq)
                    self.sync(s)
                    break
            elif token.phase == "next_hand":
                if self.clock.expired(scheduled, 30.0):
                    verdict = s.next_p2p_hand()
                    if verdict != "not_ready":
                        self.sync(s)
                        break
                    # A missing settlement report also has a30s timer.
                    # Keep inspecting it rather than retrying Next forever.
            if (s.local_seat == s.casual_timekeeper_seat()
                    and token.actor != s.local_seat
                    and token.phase != "next_hand"
                    and self.clock.expired(scheduled, 45.0 if token.phase == "action" else 30.0)):
                s.casual_drop(token.actor, "timeout", scope=asdict(token))
                self.sync(s)
                break
        current = self.view(s)
        if current != self.last_view:
            self.last_view = current
            s._notify_state_changed()


def runtime(s):
    value = getattr(s, "_casual_runtime", None)
    if value is None:
        value = s._casual_runtime = CasualRuntime(s)
    return value


def validate_scope(s, msg):
    """Exact public work position, never a receiver-local elapsed-time test."""
    raw = msg.get("scope")
    if not isinstance(raw, dict):
        return False
    try:
        token = WaitToken(**raw)
    except (TypeError, ValueError):
        return False
    if (type(token.hand) is not int or type(token.actor) is not int
            or type(token.action_seq) is not int
            or not isinstance(token.phase, str) or not isinstance(token.detail, str)
            or token.actor != msg.get("lost_seat")):
        return False
    if token in runtime(s).waits(s) and token.phase != "next_hand":
        return True
    if token.phase != "drop_ack":
        return False
    # A survivor has its own ACK before the timekeeper receives it, and
    # can therefore commit this boundary first. A missing-ACK notice still
    # names that same boundary there, including while its reserved next
    # hand is shuffling. Never revive it after that hand has been dealt or
    # a later membership boundary has committed.
    continuation = s._casual_continuation
    previous = continuation.last_notice if continuation is not None else None
    if (previous is None or continuation.epoch != token.action_seq
            or token.hand != previous["hand"]
            or token.detail != str(previous["resume_hand"])
            or token.actor not in previous["required"]
            or token.actor in s.departed_seats):
        return False
    if continuation.carry is not None:
        return True
    deal = getattr(s._deal_driver, "deal", None)
    return (s._hand_no == previous["resume_hand"] and deal is not None
            and not deal.is_shuffle_complete())


def clock_fault(s, msg):
    if (not s._casual_reliability or type(msg.get("seat")) is not int
            or type(msg.get("hand")) is not int):
        return
    seat = msg["seat"]
    if not 0 <= seat < len(s._seat_order) or seat in s.departed_seats:
        return
    # A clock fault is session scoped: a spectator host can be several hands
    # behind. It can never return, so replay only repeats the same departure.
    faults = getattr(s, "_casual_clock_faults", None)
    if faults is None:
        faults = s._casual_clock_faults = set()
    faults.add(seat)
    if seat == s._host_seat():
        s.terminate(s.HOST_LOST, "Connection to host lost (clock)")
    elif s.local_seat == s.casual_timekeeper_seat():
        s.casual_drop(seat, "clock")
