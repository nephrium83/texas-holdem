"""Pins the client<->engine contract (MULTIPLAYER.md Phase 1, section 5).

The important assertion is no-leak: a snapshot addressed to seat N must
never contain another seat's hole cards.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.engine import Engine, Player
from holdem import contract


def _table(n=4, stack=1000):
    players = [Player(i, f"P{i}", stack) for i in range(n)]
    e = Engine(players, sb=10, bb=20, structure="No-Limit")
    e.start_hand()
    return e


# --------------------------------------------------------------- shape

def test_snapshot_has_all_contract_fields():
    e = _table()
    snap = contract.build_snapshot(e, 0)
    for key in ("type", "seat", "hand_num", "street", "board", "pot",
                "button", "sb_seat", "bb_seat", "action_on", "seats", "you"):
        assert key in snap, f"snapshot missing '{key}'"
    assert snap["type"] == "snapshot"
    assert snap["seat"] == 0
    for s in snap["seats"]:
        for key in ("seat", "name", "stack", "bet", "folded", "all_in",
                    "in_seat", "sitting_out", "last_action", "pos", "is_you"):
            assert key in s, f"seat view missing '{key}'"


def test_you_hole_present_only_for_addressed_seat():
    e = _table()
    # every seat has its own hole in its own snapshot
    for seat in range(len(e.players)):
        if e.players[seat].hole:
            snap = contract.build_snapshot(e, seat)
            assert "hole" in snap["you"]
            assert len(snap["you"]["hole"]) == 2


def test_no_leak_other_seats_holes_absent():
    """THE invariant: seat N's snapshot contains no other seat's cards."""
    e = _table()
    for seat in range(len(e.players)):
        snap = contract.build_snapshot(e, seat)
        # the only cards in the snapshot are the board and *my* hole
        my_hole = set(snap["you"].get("hole", []))
        board = set(snap["board"])
        allowed = my_hole | board
        # reconstruct every card string that appears anywhere in the snapshot
        blob = repr(snap)
        # each other seat's actual hole cards must not appear in my snapshot
        for other in range(len(e.players)):
            if other == seat:
                continue
            for c in e.players[other].hole:
                cs = contract.card_str(c)
                if cs in allowed:
                    continue  # coincides with board or (impossible) my hole
                assert cs not in blob, (
                    f"LEAK: seat {other}'s card {cs} visible in seat {seat}'s snapshot")


def test_legal_present_only_on_actors_turn():
    e = _table()
    actor = e.actor
    assert actor is not None
    # actor sees legal; a non-actor does not
    snap_actor = contract.build_snapshot(e, actor)
    assert "legal" in snap_actor["you"]
    other = next(i for i in range(len(e.players)) if i != actor)
    snap_other = contract.build_snapshot(e, other)
    assert "legal" not in snap_other["you"]


def test_legal_matches_engine():
    e = _table()
    actor = e.actor
    snap = contract.build_snapshot(e, actor)
    assert snap["you"]["legal"] == e.legal(actor)


def test_position_badges_assigned():
    e = _table()
    snap = contract.build_snapshot(e, 0)
    badges = {s["pos"] for s in snap["seats"] if s["pos"]}
    # a live table always has a button; blinds present in ring games
    assert "BTN" in badges


# --------------------------------------------------------------- commands

def test_apply_command_fold_matches_direct_act():
    e = _table()
    actor = e.actor
    contract.apply_command(e, actor, "fold")
    assert e.players[actor].folded


def test_apply_command_check_call_is_legal():
    e = _table()
    actor = e.actor
    contract.apply_command(e, actor, "check_call")
    # after acting, either we matched the bet or checked; not folded
    assert not e.players[actor].folded


def test_apply_command_raise_to_is_absolute():
    e = _table()
    actor = e.actor
    lg = e.legal(actor)
    target = lg["min_to"]
    contract.apply_command(e, actor, "raise_to", {"amount": target})
    # the actor's committed bet should now equal the absolute target
    assert e.players[actor].bet == target


def test_apply_command_unknown_raises():
    e = _table()
    with pytest.raises(ValueError):
        contract.apply_command(e, 0, "teleport")


if __name__ == "__main__":
    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  {name}: ok")
            passed += 1
        except Exception as exc:
            print(f"  {name}: FAIL - {exc}")
    print(f"{passed}/{len(fns)} passed")
