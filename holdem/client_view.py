"""Client-facing view of a hostless session — the boundary the rendering
client (Godot front end, or any UI) consumes.

Architecture: the client runs NO game logic. It receives *snapshots* and
sends *commands*. The Python sidecar owns the Session (crypto + P2P +
replica betting); this module turns the sidecar's state into a per-player
snapshot the client renders, and turns client commands into session calls.
A thin socket layer (added separately) just moves these dicts as JSON.

This builds on contract.build_snapshot (the engine->client half from
MULTIPLAYER.md section 5), reusing its public per-seat view and adding the
hostless lifecycle envelope: deal progress, void, settlement, and showdown
reveals.

Hidden-information invariant (inherited and preserved): during play a
snapshot carries hole cards for the LOCAL seat only. Other seats' cards
appear solely in a contested-showdown 'settled' snapshot, where the
post-hand audit has already made them public. A client physically cannot
leak what it was never sent. Every snapshot is plain JSON-serialisable.
"""
from __future__ import annotations

from typing import Optional

from holdem import contract, player_info


def _holes_recovered(session) -> bool:
    hole = session.deal_hole_cards
    return len(hole) == 2 and all(c is not None for c in hole)


def _phase(session, dealt: bool) -> str:
    if session.hand_voided:
        return "void"
    if session.hand_result is not None:
        return "settled"
    if not dealt:
        return "dealing"
    return "betting"


def snapshot(session) -> dict:
    """Everything the local player's client needs to render right now.

    Returns a lobby snapshot when no hand is in progress, otherwise a full
    in-hand snapshot for the local seat.
    """
    replica = getattr(session, "_replica", None)
    if replica is None:
        return _lobby_snapshot(session)

    seat = session.local_seat
    engine = replica.engine
    snap = contract.build_snapshot(engine, seat)
    snap["hand_num"] = session._hand_no

    dealt = _holes_recovered(session)
    phase = _phase(session, dealt)
    snap["phase"] = phase

    # Local hole cards come from the deal, never the dummy deck the replica
    # was seeded with; omit them until they are actually recovered.
    if dealt:
        local_hole = list(session.deal_hole_cards)
        snap["you"]["hole"] = [contract.card_str(c) for c in local_hole]
        made_hand = player_info.made_hand_view(local_hole, engine.board)
        if made_hand is not None:
            snap["you"]["made_hand"] = made_hand
        else:
            snap["you"].pop("made_hand", None)
    else:
        snap["you"].pop("hole", None)
        snap["you"].pop("made_hand", None)

    # Offer legal actions only when it is genuinely this seat's turn to bet.
    if phase != "betting" or engine.actor != seat:
        snap["you"].pop("legal", None)

    # Void / settlement envelope.
    snap["voided"] = session.hand_voided
    snap["void_reason"] = session.void_reason if session.hand_voided else None
    snap["result"] = session.hand_result

    # Continuous-session envelope: whether the whole match is over (at most
    # one seat with chips) and, if so, the winning seat. Lets the client
    # show "game over" and stop offering next_hand. Absent/false mid-match.
    session_over = getattr(session, "_session_over", False)
    snap["session_over"] = session_over
    snap["session_winner"] = (getattr(session, "_session_winner", None)
                              if session_over else None)
    snap["eliminated"] = getattr(session, "_p2p_spectator", False)
    snap["final_stacks"] = (getattr(session, "_final_stacks", None)
                              if session_over else None)
    snap["turn"] = player_info.turn_view(
        engine,
        seat,
        phase=phase,
        result=session.hand_result,
        session_over=session_over,
        session_winner=snap["session_winner"],
        eliminated=snap["eliminated"],
        void_reason=snap["void_reason"],
    )
    snap["verification"] = player_info.verification_view(
        phase, snap["void_reason"]
    )
    snap["settlement"] = (
        player_info.settlement_view(
            engine,
            session.hand_result,
            seat,
            starting_stacks=getattr(session, "_hand_stacks", None),
        )
        if phase == "settled" and session.hand_result is not None
        else None
    )

    # Showdown reveals: at a contested showdown (result carries scored runs)
    # the audit has made every hole public, so the client can table them.
    # A hand that ended by folds has no runs and reveals nothing.
    result = session.hand_result
    if (phase == "settled" and result and result.get("runs")
            and session._deal_driver is not None):
        revealed = session._deal_driver.all_hole_cards()
        if revealed:
            by_seat = {s: [contract.card_str(c) for c in cards]
                       for s, cards in revealed.items()}
            for sv in snap["seats"]:
                if sv["seat"] in by_seat and not sv["is_you"]:
                    sv["hole"] = by_seat[sv["seat"]]
    return snap


def _lobby_snapshot(session) -> dict:
    """Table membership before a hand is running."""
    order = list(getattr(session, "_seat_order", []) or [])
    players = getattr(session, "players", {}) or {}
    seats = []
    for i, cid in enumerate(order):
        p = players.get(cid)
        seats.append({
            "seat": i,
            "conn_id": cid,
            "name": (p.nickname if p else ""),
            "is_you": (cid == session.local_conn_id),
        })
    my_seat = order.index(session.local_conn_id) \
        if session.local_conn_id in order else -1
    session_over = getattr(session, "_session_over", False)
    eliminated = getattr(session, "_p2p_spectator", False)
    winner = getattr(session, "_session_winner", None) if session_over else None
    if session_over:
        winner_name = (
            seats[winner]["name"]
            if winner is not None and 0 <= winner < len(seats)
            else ""
        )
        headline = (
            "You won the match"
            if winner == my_seat
            else f"{winner_name} won the match" if winner_name
            else "Match complete"
        )
        turn_state = "match_complete"
    elif eliminated:
        headline = "You are out | table still playing"
        turn_state = "eliminated"
    else:
        headline = "Waiting for players"
        turn_state = "lobby"
    return {
        "type": "snapshot",
        "phase": "lobby",
        "hand_num": getattr(session, "_hand_no", 0),
        "seats": seats,
        "you": {"seat": my_seat},
        "session_over": session_over,
        "session_winner": winner,
        "eliminated": eliminated,
        "final_stacks": (getattr(session, "_final_stacks", None)
                         if session_over else None),
        "turn": {
            "state": turn_state,
            "headline": headline,
            "street": "idle",
            "street_label": "Idle",
            "actor": -1,
            "actor_name": "",
            "pot": 0,
            "your_stack": 0,
            "your_bet": 0,
            "effective_stack": 0,
            "void_reason": None,
        },
        "verification": player_info.verification_view("lobby"),
        "events": [],
        "settlement": None,
    }


def apply_command(session, command: str,
                  payload: Optional[dict] = None) -> dict:
    """Apply a client command to the hostless session.

    Betting commands are broadcast to every replica via the session; the
    result echoes the engine's verdict so the client can surface 'not your
    turn' / 'illegal' without guessing. Legality is the replica's job, as
    for any peer action -- never trusted from the client.
    """
    payload = payload or {}
    if command == "fold":
        verdict = session.send_bet_action("fold")
    elif command == "check_call":
        # The engine treats a zero-to-call "call" as a check.
        verdict = session.send_bet_action("call")
    elif command == "raise_to":
        verdict = session.send_bet_action("raise", int(payload["amount"]))
    elif command == "next_hand":
        # Advance the continuous session past a settled/voided hand. This
        # is a table-wide step: every peer's sidecar makes the same call
        # and derives the same next hand from identical replicas. The
        # verdict ("started" / "session_over" / "eliminated" / "not_ready")
        # is reported back rather than mapped to applied/rejected, since it
        # is not a betting action.
        verdict = session.next_p2p_hand()
        return {"type": "command_result", "command": command,
                "ok": verdict in ("started", "session_over", "eliminated"),
                "verdict": verdict}
    else:
        raise ValueError(f"unknown command: {command!r}")
    return {"type": "command_result", "command": command,
            "ok": verdict == "applied", "verdict": verdict}


__all__ = ["snapshot", "apply_command"]
