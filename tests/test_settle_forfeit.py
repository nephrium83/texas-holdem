"""settle_forfeit: the casual table's settlement when a player drops.

The dropper folds and forfeits every chip it put in, a raise nobody called
included. One seat left wins the pot; two or more share each pot they are
in, as if they tied, on the engine's own side-pot layers and odd-chip rule.

The hand-worked cases drive a real Engine to the moment of the drop and
feed settle_forfeit the figures a session would read from its replica.
Every expected stack is worked out by hand in the comments. The last test
checks the same function against settle() itself over random hands.
"""
import copy
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.engine import Card, Engine, Player, settle_forfeit


def _table(stacks, sb, bb, button=None):
    ps = [Player(i, f"P{i}", s) for i, s in enumerate(stacks)]
    e = Engine(ps, sb, bb, "No-Limit", random.Random(7))
    if button is not None:
        e.button = button              # start_hand moves it one seat on
    assert e.start_hand()
    return e


def _forfeit(e, dropped):
    """settle_forfeit fed from an engine mid-hand, as a session would."""
    return settle_forfeit(
        stacks=[p.stack for p in e.players],
        committed=[p.total for p in e.players],
        in_hand=[p.idx for p in e.contested()],
        dropped=dropped, button=e.button)


def _settle_tied(e, dropped):
    """settle() on a copy of the table with `dropped` folded and a board
    that every seat left ties on: the naive forfeit."""
    t = copy.deepcopy(e)
    t.players[dropped].folded = True
    t.board = [Card(v, 3) for v in range(10, 15)]    # a royal flush
    for k, p in enumerate(t.contested()):
        p.hole = [Card(2 + k, 0), Card(2 + k, 1)]
    res = t.settle()
    return [p.stack for p in t.players], res


# ------------------------------------------------------------ hand-worked

def test_heads_up_drop_gives_the_other_seat_the_pot():
    e = _table([500, 500], 10, 20)     # seat 0 is the button and SB
    e.act(0, "call")                   # 20 each
    e.act(1, "call")
    e.next_street()
    e.act(1, "raise", 40)              # flop: 40 each, pot 120
    e.act(0, "call")
    e.next_street()
    out = _forfeit(e, 0)               # seat 0 drops on the turn
    # 0: 500 - 60;  1: 500 - 60 + 120
    assert out["stacks"] == [440, 560]
    assert out["pots"] == [{"amount": 120, "eligible": [1],
                            "payouts": {1: 120}}]


def test_uncalled_raise_is_forfeited():
    e = _table([1000, 1000], 5, 10)    # seat 0 is the button and SB
    e.act(0, "raise", 200)             # nobody has called it
    out = _forfeit(e, 0)
    # 0 keeps only what it had behind: 1000 - 200
    # 1 wins 200 + its own 10:          1000 - 10 + 210
    assert out["stacks"] == [800, 1200]

    # settle() with the dropper folded hands the raise back instead
    naive, res = _settle_tied(e, 0)
    assert res["refund"] == (0, 190)
    assert naive == [990, 1010]


def test_multiway_uncalled_raise_joins_the_top_pot():
    e = _table([500, 500, 500], 10, 20, button=2)  # btn 0, sb 1, bb 2
    e.act(0, "raise", 200)             # the blinds have not acted
    out = _forfeit(e, 0)
    # main pot  10 x 3 = 30            -> seats 1 and 2, 15 each
    # side pot  10 (0) + 10 (2) = 20   -> seat 2, the only seat left in it
    # the 180 nobody matched joins the top pot, the side pot -> seat 2
    # 0: 500 - 200;  1: 490 + 15;  2: 480 + 15 + 20 + 180
    assert out["stacks"] == [300, 505, 695]
    assert out["pots"] == [
        {"amount": 30, "eligible": [1, 2], "payouts": {1: 15, 2: 15}},
        {"amount": 200, "eligible": [2], "payouts": {2: 200}},
    ]


def test_three_way_split_returns_own_chips_and_shares_the_droppers():
    e = _table([500, 500, 500], 10, 20, button=2)  # btn 0, sb 1, bb 2
    e.act(0, "call")
    e.act(1, "call")
    e.act(2, "call")                   # 20 each
    e.next_street()
    e.act(1, "call")                   # check
    e.act(2, "raise", 60)
    e.act(0, "call")
    e.act(1, "call")                   # 80 each, pot 240
    e.next_street()
    out = _forfeit(e, 1)               # seat 1 drops on the turn
    # seats 0 and 2 each get their own 80 back plus half of seat 1's 80
    assert out["stacks"] == [540, 420, 540]
    assert out["pots"] == [{"amount": 240, "eligible": [0, 2],
                            "payouts": {0: 120, 2: 120}}]


def test_short_all_in_shares_only_the_pot_it_matched():
    e = _table([2000, 2000, 10], 5, 10, button=2)  # btn 0, sb 1, bb 2
    assert e.players[2].all_in         # the big blind took all 10
    e.act(0, "raise", 1000)
    e.act(1, "call")
    out = _forfeit(e, 0)
    # main pot  10 x 3 = 30            -> seats 1 and 2, 15 each
    # side pot  990 x 2 = 1980         -> seat 1, the only seat left in it
    # "own chips back plus an equal split" would give seat 2 10 + 500 = 510
    assert out["stacks"] == [1000, 2995, 15]
    assert out["pots"] == [
        {"amount": 30, "eligible": [1, 2], "payouts": {1: 15, 2: 15}},
        {"amount": 1980, "eligible": [1], "payouts": {1: 1980}},
    ]


def test_all_in_dropper_forfeits_too():
    e = _table([100, 500, 500], 10, 20, button=2)  # btn 0, sb 1, bb 2
    e.act(0, "raise", 100)             # all-in
    e.act(1, "call")
    e.act(2, "call")
    e.next_street()
    e.act(1, "raise", 50)
    e.act(2, "call")                   # 100 / 150 / 150
    e.next_street()
    out = _forfeit(e, 0)
    # 400 shared by seats 1 and 2; the all-in seat keeps nothing
    assert out["stacks"] == [0, 550, 550]


def test_folded_dropper_keeps_its_stack_and_the_others_split():
    e = _table([500, 500, 500], 10, 20, button=2)  # btn 0, sb 1, bb 2
    e.act(0, "call")
    e.act(1, "call")
    e.act(2, "call")                   # 20 each
    e.next_street()
    e.act(1, "raise", 40)
    e.act(2, "call")
    e.act(0, "fold")                   # 20 / 60 / 60, pot 140
    out = _forfeit(e, 0)               # the folded seat drops
    # 0's stack is what it already had; 1 and 2 split 140
    assert out["stacks"] == [480, 510, 510]
    assert out["pots"] == [{"amount": 140, "eligible": [1, 2],
                            "payouts": {1: 70, 2: 70}}]


def test_blinds_only_heads_up():
    e = _table([500, 500], 10, 20)     # seat 0 is the button and SB
    out = _forfeit(e, 0)               # drops before acting
    # 1 wins the small blind and has its own big blind back
    assert out["stacks"] == [490, 510]


def test_blinds_only_seat_that_put_nothing_in_is_in_no_pot():
    e = _table([500, 500, 500], 10, 20, button=2)  # btn 0, sb 1, bb 2
    out = _forfeit(e, 1)               # SB drops before anyone acts
    # the button has put nothing in, so it shares no pot, as at a
    # showdown; the big blind wins the small blind
    assert out["stacks"] == [500, 490, 510]
    assert out["pots"] == [{"amount": 30, "eligible": [2],
                            "payouts": {2: 30}}]


def test_seats_left_that_put_nothing_in_share_everything():
    # seat 1 is empty, so the small blind is dead; the big blind, all-in
    # for 15, is the only seat to have posted, and drops
    out = settle_forfeit(stacks=[500, 0, 0, 500], committed=[0, 0, 15, 0],
                         in_hand=[0, 2, 3], dropped=2, button=0)
    # 15 split by seats 0 and 3; the odd chip walks 1, 2, 3 -> seat 3
    assert out["stacks"] == [507, 0, 0, 508]
    assert out["pots"] == [{"amount": 15, "eligible": [0, 3],
                            "payouts": {0: 7, 3: 8}}]


def test_odd_chip_goes_to_the_first_seat_left_of_the_button():
    e = _table([500] * 4, 5, 10, button=3)  # btn 0, sb 1, bb 2, utg 3
    e.act(3, "call")
    e.act(0, "call")
    e.act(1, "fold")                   # leaves its 5
    e.act(2, "call")                   # check; pot 35
    e.next_street()
    out = _forfeit(e, 2)               # the big blind drops on the flop
    # 35 split by seats 0 and 3, 17 each; the odd chip walks forward
    # from the button 0: seat 1 folded, 2 dropped, 3 gets it
    assert out["stacks"] == [507, 495, 490, 508]


# ------------------------------------------------------------- contract

def test_changes_none_of_its_arguments():
    stacks, committed, in_hand = [800, 990, 500], [200, 10, 0], [0, 1, 2]
    args = dict(stacks=stacks, committed=committed, in_hand=in_hand,
                dropped=0, button=1)
    first = settle_forfeit(**args)
    assert (stacks, committed, in_hand) == ([800, 990, 500], [200, 10, 0],
                                            [0, 1, 2])
    assert settle_forfeit(**args) == first
    # read-only inputs work too: it never writes to them
    assert settle_forfeit(stacks=tuple(stacks), committed=tuple(committed),
                          in_hand=frozenset(in_hand), dropped=0,
                          button=1) == first


def test_dropper_alone_in_the_hand_is_refused():
    e = _table([500, 500], 10, 20)
    e.act(0, "raise", 60)
    e.act(1, "fold")                   # seat 0 has already won it
    with pytest.raises(ValueError, match="settle"):
        _forfeit(e, 0)


@pytest.mark.parametrize("bad", [
    dict(committed=[10, 20, 0]),               # one seat short
    dict(committed=[10, -20]),
    dict(stacks=[490, -1]),
    dict(in_hand=[0, 2]),
    dict(dropped=2),
    dict(button=-1),
])
def test_malformed_tables_are_refused(bad):
    args = dict(stacks=[490, 480], committed=[10, 20], in_hand=[0, 1],
                dropped=0, button=0)
    args.update(bad)
    with pytest.raises(ValueError):
        settle_forfeit(**args)


# ---------------------------------------------------- against settle()

def _random_hand(rng):
    """An engine stopped at a random point of a random legal hand."""
    n = rng.randint(2, 6)
    e = _table([rng.choice([15, 40, 120, 500, 2000]) for _ in range(n)],
               5, 10, button=rng.randrange(n))
    for _ in range(rng.randint(0, 30)):
        if len(e.contested()) <= 1 or e.street == "showdown":
            break
        if e.actor is None:
            e.next_street()
            continue
        i = e.actor
        lg = e.legal(i)
        action = rng.choice(["fold", "call"]
                            + (["raise"] if lg["can_raise"] else []))
        amount = 0
        if action == "raise":
            amount = rng.randint(lg["min_to"], lg["max_to"])
        e.act(i, action, amount)
    return e


def test_matches_settle_on_a_tie_except_the_droppers_uncalled_raise():
    """The forfeit is settle() on a board the seats left all tie on, with
    the dropper folded, except that settle() hands the dropper its
    uncalled raise back. With no dead money in these hands, the pots and
    odd chips must otherwise agree to the chip."""
    seen = {"equal": 0, "raise kept": 0, "folded dropper": 0,
            "several left": 0, "refused": 0}
    for seed in range(300):
        e = _random_hand(random.Random(seed))
        bank = sum(p.stack + p.total for p in e.players)
        in_hand = [p.idx for p in e.contested()]
        for d in [p.idx for p in e.seated()]:
            where = f"seed={seed} dropped={d}"
            if in_hand == [d]:
                with pytest.raises(ValueError):
                    _forfeit(e, d)
                seen["refused"] += 1
                continue
            out = _forfeit(e, d)["stacks"]
            assert sum(out) == bank, where
            assert out[d] == e.players[d].stack, f"{where}: dropper paid"
            seen["folded dropper"] += d not in in_hand
            seen["several left"] += len(set(in_hand) - {d}) > 1

            naive, res = _settle_tied(e, d)
            if res["refund"] is not None and res["refund"][0] == d:
                back = res["refund"][1]
                assert naive[d] == out[d] + back, where
                gains = [out[i] - naive[i] for i in range(len(out)) if i != d]
                assert min(gains) >= 0 and sum(gains) == back, where
                seen["raise kept"] += 1
            else:
                assert out == naive, where
                seen["equal"] += 1
    assert min(seen.values()) >= 20, seen
