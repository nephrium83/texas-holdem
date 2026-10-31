"""Engine.act enforces turn order and legality (audit finding 1).

act() used to compute legal() but never check that the caller was the
seat to act, that the seat could act at all, or that a raise was legal;
an unrecognised action still advanced the actor. Every refusal must
raise IllegalAction AND leave the engine exactly as it was.
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.engine import Engine, IllegalAction, Player


def _engine(stacks=(1000, 1000, 1000)):
    ps = [Player(i, f"P{i}", s) for i, s in enumerate(stacks)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(7))
    e.button = len(ps) - 1          # -> btn 0, first actor 0 (3-handed)
    assert e.start_hand()
    return e


def _snapshot(e):
    return (e.actor, e.current_bet, e.min_raise, e.pot,
            sorted(e.need_to_act), sorted(e.no_raise),
            [(p.stack, p.bet, p.folded, p.all_in, p.last_action)
             for p in e.players])


def test_out_of_turn_action_is_refused_and_changes_nothing():
    e = _engine()
    actor = e.actor
    other = (actor + 1) % len(e.players)
    before = _snapshot(e)
    for action in ("fold", "call", "raise"):
        with pytest.raises(IllegalAction, match="out of turn"):
            e.act(other, action, 100)
    assert _snapshot(e) == before


def test_unknown_action_is_refused_and_does_not_advance_the_actor():
    e = _engine()
    before = _snapshot(e)
    for bogus in ("check", "allin", "", None, "FOLD"):
        with pytest.raises(IllegalAction, match="unknown action"):
            e.act(e.actor, bogus)
    assert _snapshot(e) == before


def test_bad_seat_index_is_refused():
    e = _engine()
    before = _snapshot(e)
    for seat in (-1, 3, 99, "0"):
        with pytest.raises(IllegalAction):
            e.act(seat, "call")
    assert _snapshot(e) == before


def test_illegal_raise_is_refused():
    """After an all-in under-raise the original raiser may call but not
    re-raise; the engine itself must enforce that, not just legal()."""
    ps = [Player(0, "P0", 1000), Player(1, "P1", 95), Player(2, "P2", 1000)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(1))
    e.button = 2
    e.start_hand()
    e.act(0, "raise", 60)
    e.act(1, "raise", 95)            # all-in under-raise
    e.act(2, "fold")
    assert e.actor == 0 and not e.legal(0)["can_raise"]
    before = _snapshot(e)
    with pytest.raises(IllegalAction, match="may not raise"):
        e.act(0, "raise", 500)
    assert _snapshot(e) == before
    e.act(0, "call")                 # the legal option still works
    assert e.actor is None


def test_no_action_once_the_round_is_closed():
    e = _engine()
    while e.actor is not None:
        e.act(e.actor, "call")
    before = _snapshot(e)
    for seat in range(len(e.players)):
        with pytest.raises(IllegalAction, match="out of turn"):
            e.act(seat, "call")
    assert _snapshot(e) == before


def test_folded_seat_cannot_act_even_if_named_actor():
    e = _engine()
    seat = e.actor
    e.act(seat, "fold")
    e.actor = seat                   # simulate a corrupted / stale actor
    with pytest.raises(IllegalAction, match="cannot act"):
        e.act(seat, "call")


def test_legal_actions_still_work_end_to_end():
    e = _engine()
    e.act(e.actor, "raise", 60)
    e.act(e.actor, "call")
    e.act(e.actor, "fold")
    assert e.actor is None
