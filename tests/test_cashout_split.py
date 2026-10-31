"""EV cashout respects side-pot eligibility (audit finding 6).

The cashout used ``int(hero_eq * e.pot)`` -- equity against the WHOLE pot,
including uncalled chips and side pots the hero is not in -- and split the
rest evenly over every opponent. cashout_split applies settle()'s own rules.
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.engine import Engine, Player, cashout_split


def _jam_all(stacks):
    """Everyone shoves (or calls a shove they cannot raise)."""
    ps = [Player(i, f"P{i}", s) for i, s in enumerate(stacks)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(3))
    e.button = len(ps) - 1
    assert e.start_hand()
    while e.actor is not None:
        lg = e.legal(e.actor)
        if lg["can_raise"]:
            e.act(e.actor, "raise", lg["max_to"])
        else:
            e.act(e.actor, "call")
    return e


def _contenders(e):
    return [p.idx for p in e.contested()]


def _conserved(e, split):
    back = split["refund"][1] if split["refund"] else 0
    return sum(split["payouts"].values()) + back == e.pot


def test_short_hero_is_priced_against_the_pot_it_can_win():
    """Heads-up: hero has 50, villain shoves 300. 250 is uncalled and goes
    straight back; the hero can only ever win 100. The old formula priced
    80% of 350 = 280 -- more than the whole pot the hero is playing for."""
    e = _jam_all([50, 300])
    split = cashout_split(e.players, 0, _contenders(e), 0.80)
    assert split["refund"] == (1, 250)
    assert split["eligible_pot"] == 100
    assert split["gross"] == 80
    assert split["gross"] < int(0.80 * e.pot)       # the old, wrong number
    assert split["payouts"][0] == split["net"] <= 100
    assert _conserved(e, split)


def test_mid_stack_hero_shares_main_pot_only_with_who_is_in_it():
    """Short 50 / Mid 120 / Big 300 all in. Main 150 [all], side 140
    [mid, big], 180 uncalled back to big. Hero = mid is in both pots."""
    e = _jam_all([50, 120, 300])
    split = cashout_split(e.players, 1, _contenders(e), 0.60)
    assert split["refund"] == (2, 180)
    assert split["eligible_pot"] == 290
    assert split["gross"] == int(0.60 * 290)
    # the short stack is eligible for the 150 main pot ONLY
    assert split["payouts"][0] <= 150
    assert _conserved(e, split)
    assert split["payouts"][1] == split["net"]


def test_no_cashout_when_others_contest_a_side_pot():
    """Hero = short. The 140 side pot is fought over by mid and big alone:
    only a run-out can decide it, so no cashout may be offered."""
    e = _jam_all([50, 120, 300])
    assert cashout_split(e.players, 0, _contenders(e), 0.90) is None


def test_fee_and_payouts_add_up_exactly():
    for stacks, hero, eq in (([50, 300], 0, 0.71), ([400, 400], 1, 0.66),
                             ([120, 300, 300], 0, 0.58)):
        e = _jam_all(stacks)
        split = cashout_split(e.players, hero, _contenders(e), eq)
        if split is None:
            continue
        assert split["fee"] == split["gross"] - split["net"]
        assert split["net"] <= split["gross"] <= split["eligible_pot"]
        assert all(v >= 0 for v in split["payouts"].values())
        assert _conserved(e, split)


def test_equal_stacks_heads_up_matches_the_simple_formula():
    """With nothing uncalled and no side pot, eligible pot == whole pot."""
    e = _jam_all([400, 400])
    split = cashout_split(e.players, 0, _contenders(e), 0.66)
    assert split["refund"] is None
    assert split["eligible_pot"] == e.pot == 800
    assert split["gross"] == int(0.66 * 800)
