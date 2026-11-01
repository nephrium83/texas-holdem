"""Engine betting rules against the TDA 2024 text the rules profile adopts
(docs/research/sources/poker-tda-2024-v1.0-longform.txt).

Audit findings #36 (short big blind lowered the bring-in), #37 (= profile gap
B6: cumulative short all-ins never reopened betting), #38 (deck injection
accepted malformed decks) and #136 (= profile gap C2: out-of-range raises
were clamped and applied instead of rejected). The Rule 47 and Rule 43
illustrations are reproduced with the exact numbers from the source.
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.engine import Deck, Engine, IllegalAction, Player


def _table(stacks, sb, bb, button_after):
    """Engine with seats 0..n-1; start_hand advances the button by one, so
    we set it one seat earlier to land on ``button_after``."""
    ps = [Player(i, chr(65 + i), s) for i, s in enumerate(stacks)]
    e = Engine(ps, sb, bb, "No-Limit", random.Random(1))
    e.button = (button_after - 1) % len(ps)
    assert e.start_hand()
    assert e.button == button_after
    return e


def _to_flop(e):
    """Everyone just calls preflop, then deal the flop."""
    while e.actor is not None:
        e.act(e.actor, "call")
    e.next_street()


def _act(e, seat, action, amount=0):
    assert e.actor == seat, f"expected seat {seat} to act, actor is {e.actor}"
    e.act(seat, action, amount)


def _snap(e):
    return (e.actor, e.current_bet, e.min_raise, e.pot, sorted(e.need_to_act),
            sorted(e.no_raise), dict(e.acted_at),
            [(p.stack, p.bet, p.folded, p.all_in, p.last_action) for p in e.players],
            len(e.public_events))


# ------------------------------------------------------- Rule 47 (B6, #37)

def _rule47_example1():
    """NLHE 50-100. Post-flop A opens 100, B all-in 125, C calls 125,
    D all-in 200, E calls 200.  Seats: A0 B1 C2 D3 E4, button E."""
    big = 100_000
    e = _table([big, 225, big, 300, big], 50, 100, button_after=4)
    _to_flop(e)                       # everyone paid 100 preflop
    _act(e, 0, "raise", 100)          # A opens for the minimum
    _act(e, 1, "raise", 125)          # B all-in: +25, not a full raise
    _act(e, 2, "call")                # C calls 125
    _act(e, 3, "raise", 200)          # D all-in: +75, not a full raise
    _act(e, 4, "call")                # E calls 200
    return e


def test_rule47_ex1_cumulative_short_allins_reopen_for_A():
    e = _rule47_example1()
    assert e.actor == 0
    lg = e.legal(0)
    assert lg["can_raise"], "25 + 75 = a full 100 raise: betting reopens for A"
    assert lg["to_call"] == 100
    assert lg["min_to"] == 300        # min raise stays the last FULL bet: 100
    assert e.min_raise == 100


def test_rule47_ex1a_smooth_call_leaves_C_unable_to_raise():
    e = _rule47_example1()
    _act(e, 0, "call")                # A smooth-calls 200
    # B and D are all-in; C called 125 and now faces only 75 more.
    assert e.actor == 2
    lg = e.legal(2)
    assert lg["to_call"] == 75
    assert not lg["can_raise"], "75 is not a full raise: C may only call or fold"
    with pytest.raises(IllegalAction):
        e.act(2, "raise", 300)


def test_rule47_ex1b_full_raise_reopens_for_C():
    e = _rule47_example1()
    _act(e, 0, "raise", 300)          # A raises the minimum: 100 more
    assert e.actor == 2
    lg = e.legal(2)
    assert lg["to_call"] == 175       # C already called 125
    assert lg["can_raise"], "C now faces more than a full raise"


def test_rule47_ex2_min_raise_stays_the_last_full_bet():
    """A opens 300, B all-in 500, C all-in 650, D all-in 800, E calls 800:
    F's minimum raise is still 300, i.e. to 1100.  Seats A0..F5, button F."""
    big = 100_000
    e = _table([big, 600, 750, 900, big, big], 50, 100, button_after=5)
    _to_flop(e)
    _act(e, 0, "raise", 300)
    _act(e, 1, "raise", 500)
    _act(e, 2, "raise", 650)
    _act(e, 3, "raise", 800)
    _act(e, 4, "call")
    assert e.actor == 5
    lg = e.legal(5)
    assert lg["to_call"] == 800
    assert lg["min_to"] == 1100
    assert e.min_raise == 300


def _rule47_example3():
    """NLHE 2000-4000, pre-flop. A calls 4000, B folds, C all-in for 7500
    (3500 more: not a full raise), the SB folds.  Seats SB0 BB1 A2 B3 C4."""
    big = 1_000_000
    e = _table([big, big, big, big, 7500], 2000, 4000, button_after=4)
    _act(e, 2, "call")
    _act(e, 3, "fold")
    _act(e, 4, "raise", 7500)
    _act(e, 0, "fold")
    return e


def test_rule47_ex3a_bb_may_raise_but_A_may_not_after_a_smooth_call():
    e = _rule47_example3()
    assert e.actor == 1
    lg = e.legal(1)                   # the BB has not yet acted on its option
    assert lg["can_raise"] and lg["min_to"] == 11_500
    _act(e, 1, "call")
    assert e.actor == 2
    lg = e.legal(2)
    assert lg["to_call"] == 3500
    assert not lg["can_raise"], "A faces 3500, not a full raise"


def test_rule47_ex3b_bb_min_raise_reopens_for_A():
    e = _rule47_example3()
    _act(e, 1, "raise", 11_500)
    assert e.actor == 2
    lg = e.legal(2)
    assert lg["to_call"] == 7500
    assert lg["can_raise"], "7500 is more than a full raise: reopened for A"


def test_single_short_allin_still_does_not_reopen():
    """The pre-existing (conformant) half of 47-A stays true."""
    big = 100_000
    e = _table([big, 225, big], 50, 100, button_after=2)
    _to_flop(e)
    _act(e, 0, "raise", 100)
    _act(e, 1, "raise", 125)          # +25 only
    _act(e, 2, "call")
    assert e.actor == 0
    lg = e.legal(0)
    assert lg["to_call"] == 25 and not lg["can_raise"]


def test_rule43_ex2_short_allin_does_not_lower_the_min_raise():
    """50-100 pre-flop: UTG all-in to 150 (+50). The 100 is still the largest
    bet, so a re-raise must go to at least 250. This is also why the engine
    keeps requiring a full raise above a short opening all-in rather than
    letting the next player 'complete' it -- TDA has no completion in NL."""
    big = 100_000
    e = _table([big, big, 150, big], 50, 100, button_after=3)
    _act(e, 2, "raise", 150)
    assert e.actor == 3
    assert e.legal(3)["min_to"] == 250


# ------------------------------------------------- short big blind (#36)

def test_short_allin_big_blind_keeps_the_full_bring_in_multiway():
    """10-20 with the big blind holding only 5: callers owe the full 20, the
    minimum raise is to 40, and settlement layers a 15 main pot (all three)
    under a side pot the short blind cannot win."""
    big = 1000
    e = _table([big, 5, big], 10, 20, button_after=2)   # SB0 BB1 BTN2
    bbp = e.players[1]
    assert bbp.all_in and bbp.bet == 5
    assert e.current_bet == 20
    assert e.actor == 2                                  # button acts first
    lg = e.legal(2)
    assert lg["to_call"] == 20 and lg["min_to"] == 40
    _act(e, 2, "call")
    assert e.legal(0)["to_call"] == 10                   # SB completes to 20
    _act(e, 0, "call")
    assert e.actor is None                               # BB is all-in: done
    assert e.pot == 45
    total = sum(p.stack + p.total_live + p.total_dead for p in e.players)
    while e.street != "river":
        e.next_street()
    res = e.settle()
    amounts = sorted((pot["amount"], tuple(pot["eligible"])) for pot in res["pots"])
    assert amounts == [(15, (0, 1, 2)), (30, (0, 2))]
    assert sum(p.stack for p in e.players) == total


def test_short_allin_big_blind_heads_up_small_blind_owes_the_full_blind():
    """Heads-up the button posts the SB and acts first. Facing a BB all-in
    for 5 at 10-20 it still owes the full blind (10 more) -- and may fold.
    Whatever the short blind cannot match comes back as an uncalled bet."""
    e = _table([1000, 5], 10, 20, button_after=0)       # BTN/SB0, BB1
    assert e.current_bet == 20 and e.actor == 0
    assert e.legal(0)["to_call"] == 10
    _act(e, 0, "call")
    assert e.actor is None
    while e.street != "river":
        e.next_street()
    res = e.settle()
    assert res["refund"] == (0, 15)                      # 20 vs 5: 15 back
    assert sum(p.stack for p in e.players) == 1005


def test_full_big_blind_unchanged_when_bb_posts_in_full():
    e = _table([1000, 1000, 1000], 10, 20, button_after=2)
    assert e.current_bet == 20 and e.legal(2)["to_call"] == 20


# ------------------------------------------- raise range rejected (C2, #136)

@pytest.mark.parametrize("bad", ["below", "above", -1, 0, 10**12, 2.5,
                                 float("nan"), float("inf"), True, "60", None])
def test_out_of_range_or_malformed_raise_is_rejected_untouched(bad):
    e = _table([1000, 1000, 1000], 10, 20, button_after=2)
    lg = e.legal(e.actor)
    amount = {"below": lg["min_to"] - 1, "above": lg["max_to"] + 1}.get(bad, bad)
    before = _snap(e)
    with pytest.raises(IllegalAction):
        e.act(e.actor, "raise", amount)
    assert _snap(e) == before


def test_boundary_raises_are_accepted():
    for pick in ("min_to", "max_to"):
        e = _table([1000, 1000, 1000], 10, 20, button_after=2)
        seat = e.actor
        target = e.legal(seat)[pick]
        e.act(seat, "raise", target)
        assert e.players[seat].bet == target


def test_integral_float_raise_is_accepted():
    e = _table([1000, 1000, 1000], 10, 20, button_after=2)
    seat = e.actor
    e.act(seat, "raise", 60.0)
    assert e.players[seat].bet == 60


def test_fixed_limit_raise_amount_is_ignored():
    """Fixed-Limit has exactly one legal raise size, so the amount carries no
    meaning and is not validated (the engine uses the fixed size)."""
    ps = [Player(i, chr(65 + i), 1000) for i in range(3)]
    e = Engine(ps, 10, 20, "Fixed-Limit", random.Random(1))
    e.button = 1
    e.start_hand()
    seat = e.actor
    e.act(seat, "raise", 0)
    assert e.players[seat].bet == 40


# ---------------------------------------------------- deck injection (#38)

@pytest.mark.parametrize("indices", [
    list(range(51)),                          # missing a card
    list(range(52)) + [0],                    # extra card
    [0] + list(range(1, 51)) + [0],           # duplicate
    [-1] + list(range(1, 52)),                # negative (would wrap)
    list(range(1, 53)),                       # out of range
    [True] + list(range(1, 52)),              # bool masquerading as 1
    [float(i) for i in range(52)],            # non-int
])
def test_deck_injection_rejects_malformed_indices(indices):
    with pytest.raises(ValueError):
        Deck.from_indices(indices)


def test_deck_injection_accepts_a_permutation_and_deals_index_zero_first():
    order = list(range(52))
    random.Random(4).shuffle(order)
    d = Deck.from_indices(order)
    first = d.deal(1)[0]
    assert (first.v - 2) * 4 + first.s == order[0]
    assert len(d.cards) == 51


def test_deal_refuses_to_come_up_short():
    d = Deck.from_indices(list(range(52)))
    d.deal(50)
    assert d.deal(0) == []
    with pytest.raises(ValueError):
        d.deal(3)
    assert len(d.cards) == 2                  # refusal took nothing
    with pytest.raises(ValueError):
        d.deal(-1)
