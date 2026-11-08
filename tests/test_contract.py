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
                "button", "sb_seat", "bb_seat", "action_on", "seats", "you",
                "turn", "events"):
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
    assert snap["turn"]["state"] == "your_turn"
    assert snap["turn"]["decision"]["to_call"] == e.legal(actor)["to_call"]


def test_current_hand_events_are_sequenced():
    e = _table()
    snap = contract.build_snapshot(e, e.actor)
    assert snap["events"][0]["event"] == "hand_started"
    assert [event["seq"] for event in snap["events"]] == list(
        range(1, len(snap["events"]) + 1)
    )


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


@pytest.mark.parametrize("checking", [False, True], ids=["call", "check"])
def test_apply_command_check_call_is_legal(checking):
    e = _table()
    if checking:
        # Reach the big blind's option using real calls, not synthetic state.
        while e.legal(e.actor)["to_call"]:
            e.act(e.actor, "call")
    actor = e.actor
    p = e.players[actor]
    owed = e.legal(actor)["to_call"]
    assert (owed == 0) is checking
    stack, bet, pot = p.stack, p.bet, e.pot
    event_count = len(e.public_events)
    contract.apply_command(e, actor, "check_call")
    assert not p.folded
    assert (p.stack, p.bet, e.pot) == (stack - owed, bet + owed, pot + owed)
    assert e.actor != actor
    assert len(e.public_events) == event_count + 1
    event = e.public_events[-1]
    assert (event["event"], event["seat"], event["action"], event["amount"]) == (
        "action", actor, "check" if checking else "call", owed)
    assert p.last_action == ("CHECK" if checking else f"CALL {owed}")


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


@pytest.mark.parametrize('seat', [-1, -4, 4, 100, True, False, None, '0', 0.0])
def test_invalid_snapshot_seat_refuses_before_selecting_private_cards(seat):
    e = _table()
    with pytest.raises(ValueError, match='seat'):
        contract.build_snapshot(e, seat)


@pytest.mark.parametrize('command,payload', [
    ('raise_to', {'amount': '40'}), ('raise_to', {'amount': True}),
    ('raise_to', {'amount': 40.9}), ('raise_to', {'amount': 40.0}),
    ('raise_to', {'amount': None}), ('raise_to', {}),
    ('add_chips', {'amount': '100'}), ('add_chips', {'amount': False}),
    ('sit_in', {'post_now': 'false'}), ('sit_in', {'post_now': 0}),
    ('fold', []), ('fold', False), ('check_call', {'amount': 1}),
])
def test_malformed_command_values_cannot_mutate_the_table(command, payload):
    e = _table()
    actor = e.actor
    before = [contract.build_snapshot(e, seat) for seat in range(len(e.players))]
    with pytest.raises(ValueError):
        contract.apply_command(e, actor, command, payload)
    assert before == [contract.build_snapshot(e, seat) for seat in range(len(e.players))]


@pytest.mark.parametrize('seat', [-1, True, False, 4, None, '0', 0.0])
def test_invalid_command_seat_cannot_select_another_player(seat):
    e = _table()
    before = contract.build_snapshot(e, 0)
    with pytest.raises(ValueError, match='seat'):
        contract.apply_command(e, seat, 'sit_out')
    assert contract.build_snapshot(e, 0) == before


@pytest.mark.parametrize('post_now', [False, True])
def test_sit_in_preserves_real_boolean_values(monkeypatch, post_now):
    e = _table()
    calls = []
    monkeypatch.setattr(e, 'sit_in', lambda seat, **payload: calls.append((seat, payload)))
    contract.apply_command(e, 0, 'sit_in', {'post_now': post_now})
    assert calls == [(0, {'post_now': post_now})]


def test_sit_in_omitted_post_now_stays_false(monkeypatch):
    e = _table()
    calls = []
    monkeypatch.setattr(e, 'sit_in', lambda seat, **payload: calls.append((seat, payload)))
    contract.apply_command(e, 0, 'sit_in')
    assert calls == [(0, {'post_now': False})]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, *sys.argv[1:]]))
