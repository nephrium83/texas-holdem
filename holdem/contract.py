"""Client <-> engine contract (MULTIPLAYER.md Phase 1, section 5).

The rendering client (Godot front end, or the Tkinter dev harness) never
runs game logic. It sends *commands* and receives *snapshots* and
*events*. This module implements the engine->client half: turning
authoritative engine state into a per-seat snapshot dict.

The single most important property here is the hidden-information
invariant: ``build_snapshot(engine, seat)`` includes hole cards ONLY for
``seat`` itself. A client physically cannot leak what it was never given.
"""
from __future__ import annotations

from typing import Optional

from .player_info import card_str, event_views, made_hand_view, turn_view


def _pos_badge(engine, seat: int) -> Optional[str]:
    """SB / BB / BTN badge for a seat, or None."""
    if seat == getattr(engine, "button", -1):
        return "BTN"
    if seat == getattr(engine, "sb_seat", getattr(engine, "sb_i", -1)):
        return "SB"
    if seat == getattr(engine, "bb_seat", getattr(engine, "bb_i", -1)):
        return "BB"
    return None


def _seat_view(engine, p) -> dict:
    """Public per-seat data — never includes hole cards."""
    return {
        "seat": p.idx,
        "name": p.name,
        "stack": p.stack,
        "bet": p.bet,
        "folded": p.folded,
        "all_in": p.all_in,
        "in_seat": p.in_seat,
        "sitting_out": p.sitting_out,
        "last_action": p.last_action or "",
        "pos": _pos_badge(engine, p.idx),
    }


def build_snapshot(engine, seat: int) -> dict:
    """Full renderable state for exactly one ``seat`` (section 5 snapshot).

    Hole cards for any seat other than ``seat`` are omitted entirely.
    ``you.legal`` is populated only when it is ``seat``'s turn to act.
    """
    sb_seat = getattr(engine, "sb_seat", getattr(engine, "sb_i", -1))
    bb_seat = getattr(engine, "bb_seat", getattr(engine, "bb_i", -1))

    seats = [_seat_view(engine, p) for p in engine.players]
    for s in seats:
        s["is_you"] = (s["seat"] == seat)

    me = engine.players[seat]
    you: dict = {}
    if me.hole:
        you["hole"] = [card_str(c) for c in me.hole]
        made_hand = made_hand_view(me.hole, engine.board)
        if made_hand is not None:
            you["made_hand"] = made_hand
    action_on = engine.actor if engine.actor is not None else -1
    if action_on == seat:
        you["legal"] = engine.legal(seat)

    return {
        "type": "snapshot",
        "seat": seat,
        "hand_num": getattr(engine, "hand_no", 0),
        "street": engine.street,
        "board": [card_str(c) for c in engine.board],
        "pot": engine.pot,
        "button": getattr(engine, "button", -1),
        "sb_seat": sb_seat,
        "bb_seat": bb_seat,
        "action_on": action_on,
        "seats": seats,
        "you": you,
        "turn": turn_view(engine, seat),
        "events": event_views(getattr(engine, "public_events", [])),
    }


# Command dispatch (client -> engine). The closed set from section 5,
# mapped onto the engine's act()/sit_out()/etc. Every command is validated
# by the engine exactly as a peer action is; an out-of-turn or illegal
# command raises or is rejected, never trusted.

_ABSOLUTE_RAISE = True  # raise_to carries an absolute target, not a delta


def apply_command(engine, seat: int, command: str, payload: dict | None = None):
    """Apply a section-5 command to the engine on behalf of ``seat``.

    Returns whatever the underlying engine call returns. Raises ValueError
    on an unknown command. Legality is the engine's job, not ours.
    """
    payload = payload or {}
    if command == "fold":
        return engine.act(seat, "fold")
    if command == "check_call":
        lg = engine.legal(seat)
        return engine.act(seat, "check" if lg["can_check"] else "call")
    if command == "raise_to":
        return engine.act(seat, "raise", int(payload["amount"]))
    if command == "sit_out":
        return engine.sit_out(seat)
    if command == "sit_in":
        return engine.sit_in(seat, post_now=bool(payload.get("post_now", False)))
    if command == "add_chips":
        return engine.add_chips(seat, int(payload["amount"]))
    raise ValueError(f"unknown command: {command!r}")
