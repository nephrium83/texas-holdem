"""Pins holdem/p2p/replica_table.py -- hostless betting via replica engines.

The property everything rests on: identical replicas fed the same actions
in the same order stay in PERFECT sync (state_digest equality after every
step). The seeded fuzz test is simultaneously the engine-determinism proof
(no hidden randomness in the betting path) and the replica-sync proof.
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.engine import Card, FULL_DECK
from holdem.p2p.replica_table import (
    ReplicaTable, ChipConservationError, PHASE_BETTING, PHASE_STREET_OVER,
    PHASE_SHOWDOWN, PHASE_HAND_OVER, PHASE_SETTLED)


def make_replicas(n_replicas, names, stacks, sb=5, bb=10, hand=1, button=0):
    reps = [ReplicaTable(session_id="tbl", hand_no=hand, names=list(names),
                         stacks=list(stacks), sb=sb, bb=bb)
            for _ in range(n_replicas)]
    for r in reps:
        r.start_hand(button)
    return reps


def assert_synced(reps):
    digests = [r.state_digest() for r in reps]
    assert len(set(digests)) == 1, f"replicas diverged: {digests}"


def apply_all(reps, seq, seat, action, amount=0):
    verdicts = [r.apply_action(seq, seat, action, amount) for r in reps]
    assert len(set(verdicts)) == 1, f"verdicts diverged: {verdicts}"
    assert_synced(reps)
    return verdicts[0]


def C(label):
    """'As' -> Card, for readable test fixtures."""
    v = "23456789TJQKA".index(label[0]) + 2
    s = "cdhs".index(label[1])
    return Card(v, s)


# ------------------------------------------------------------- basics

def test_replicas_start_identical():
    reps = make_replicas(3, ["A", "B", "C"], [500, 500, 500])
    assert_synced(reps)
    assert len({r.button for r in reps}) == 1
    assert len({r.actor for r in reps}) == 1
    assert all(r.phase == PHASE_BETTING for r in reps)


def test_own_hole_overwrite_does_not_desync():
    """Hole cards differ per replica pre-audit BY DESIGN; the digest must
    not see them."""
    reps = make_replicas(2, ["A", "B", "C"], [500, 500, 500])
    reps[0].set_own_hole(0, [C("As"), C("Ah")])
    reps[1].set_own_hole(1, [C("Ks"), C("Kh")])
    assert_synced(reps)


# ------------------------------------------------------------- scripted hand

def test_scripted_hand_to_showdown():
    """A full hand -- calls preflop, betting on the flop, checks down --
    with a known board and holes: replicas agree at every step, the board
    is the injected real board, and the settled result is identical with
    the right winner."""
    reps = make_replicas(3, ["A", "B", "C"], [500, 500, 500])
    seq = 0
    # preflop: everyone calls/checks around
    while reps[0].phase == PHASE_BETTING:
        assert apply_all(reps, seq, reps[0].actor, "call") == "applied"
        seq += 1
    assert reps[0].phase == PHASE_STREET_OVER

    flop = [C("2c"), C("7d"), C("Th")]
    for r in reps:
        r.advance_street(flop)
    assert_synced(reps)
    for r in reps:
        assert [(c.v, c.s) for c in r.engine.board] == [(c.v, c.s) for c in flop]

    # flop: first actor bets 40, others call
    assert apply_all(reps, seq, reps[0].actor, "raise", 40) == "applied"; seq += 1
    while reps[0].phase == PHASE_BETTING:
        assert apply_all(reps, seq, reps[0].actor, "call") == "applied"
        seq += 1

    for street_cards in ([C("Js")], [C("3h")]):
        assert reps[0].phase == PHASE_STREET_OVER
        for r in reps:
            r.advance_street(street_cards)
        assert_synced(reps)
        while reps[0].phase == PHASE_BETTING:
            assert apply_all(reps, seq, reps[0].actor, "call") == "applied"
            seq += 1

    assert reps[0].phase == PHASE_SHOWDOWN
    holes = {0: [C("As"), C("Ah")],       # aces -- the winner
             1: [C("Kd"), C("Qd")],
             2: [C("9c"), C("9s")]}
    results = []
    for r in reps:
        r.set_all_holes(holes)
        results.append(r.finish())
    assert_synced(reps)
    assert all(res == results[0] for res in results)
    assert results[0]["winners"] == [0]                 # aces win
    assert all(r.phase == PHASE_SETTLED for r in reps)
    # chip conservation: settle credits the pot back to stacks
    assert sum(p.stack for p in reps[0].engine.players) == 1500


def test_fold_out_needs_no_cards():
    """Everyone folds to one player: settle needs no board, no holes."""
    reps = make_replicas(3, ["A", "B", "C"], [500, 500, 500])
    seq = 0
    while reps[0].phase == PHASE_BETTING:
        assert apply_all(reps, seq, reps[0].actor, "fold") == "applied"
        seq += 1
    assert reps[0].phase == PHASE_HAND_OVER
    results = [r.finish() for r in reps]
    assert_synced(reps)
    assert all(res == results[0] for res in results)
    assert len(results[0]["winners"]) == 1


def test_all_in_lockup_and_side_pots():
    """Short/medium/deep stacks all-in preflop: betting locks, streets
    advance with no actors, showdown settles layered side pots identically."""
    reps = make_replicas(3, ["A", "B", "C"], [200, 60, 120])
    seq = 0
    # drive everyone all-in / calling all-in
    while reps[0].phase == PHASE_BETTING:
        seat = reps[0].actor
        lg = reps[0].engine.legal(seat)
        if lg["can_raise"]:
            assert apply_all(reps, seq, seat, "raise", lg["max_to"]) == "applied"
        else:
            assert apply_all(reps, seq, seat, "call") == "applied"
        seq += 1
    # betting locked: every street closes instantly with no actors
    for cards in ([C("2c"), C("5d"), C("7h")], [C("9s")], [C("Jc")]):
        assert reps[0].phase == PHASE_STREET_OVER
        for r in reps:
            r.advance_street(cards)
        assert_synced(reps)
    assert reps[0].phase == PHASE_SHOWDOWN
    holes = {0: [C("Qs"), C("Qh")],
             1: [C("As"), C("Ah")],       # short stack wins the main pot
             2: [C("Ks"), C("Kh")]}       # medium wins the side pot
    results = []
    for r in reps:
        r.set_all_holes(holes)
        results.append(r.finish(force_tabled=True))
    assert_synced(reps)
    assert all(res == results[0] for res in results)
    assert len(results[0]["pots"]) == 2
    assert results[0]["pots"][0]["eligible"] == [0, 1, 2]
    assert results[0]["pots"][1]["eligible"] == [0, 2]


# ------------------------------------------------------------- exact payouts
#
# The side-pot tests above assert pot shape. These assert the money: fixed
# hole cards and a fixed board, so the payout is known in advance and was
# derived by hand from the rules, not read back from the engine.
#
# Every case is three-handed with sb 5 / bb 10 and start_hand(button=0). On
# a first hand the engine moves the button one seat, so seat 1 has the
# button, seat 2 posts the small blind, seat 0 the big blind, and seat 1
# acts first. Seat 0 (A) is the short stack, seat 1 (B) the middle, seat 2
# (C) the deep one.

BOARD = ["2c", "7d", "9h", "Js", "3d"]          # no pair, flush or straight


def _shove(r, seat):
    lg = r.engine.legal(seat)
    return ("raise", lg["max_to"]) if lg["can_raise"] else ("call", 0)


def _b_raises_then_folds_to_c(r, seat):
    """B opens to 250 and folds to C's shove; A calls all-in for 100."""
    if seat == 1:
        return ("raise", 250) if r.engine.current_bet <= 10 else ("fold", 0)
    return _shove(r, seat)


def play_out(stacks, holes, board, script):
    """Three replicas play one scripted hand to settlement; returns the
    agreed (stacks, result)."""
    reps = make_replicas(3, ["A", "B", "C"], stacks)
    e = reps[0].engine
    assert (e.button, e.sb_seat, e.bb_seat, reps[0].actor) == (1, 2, 0, 1)
    seq = 0
    streets = [board[:3], board[3:4], board[4:5]]
    while reps[0].phase in (PHASE_BETTING, PHASE_STREET_OVER):
        if reps[0].phase == PHASE_BETTING:
            seat = reps[0].actor
            action, amount = script(reps[0], seat)
            assert apply_all(reps, seq, seat, action, amount) == "applied"
            seq += 1
        else:
            cards = [C(c) for c in streets.pop(0)]
            for r in reps:
                r.advance_street(cards)
            assert_synced(reps)
    assert reps[0].phase == PHASE_SHOWDOWN
    results = []
    for r in reps:
        r.set_all_holes({s: [C(a), C(b)] for s, (a, b) in holes.items()})
        results.append(r.finish(force_tabled=True))
    assert_synced(reps)
    assert all(res == results[0] for res in results)
    return reps[0].stacks, results[0]


def pots_of(result):
    """(amount, eligible, {seat: payout}) per pot, in layer order."""
    return [(p["amount"], p["eligible"], p["runs"][0]["payouts"])
            for p in result["pots"]]


# All three shove [100, 300, 500]. B's shove to 300 is a full raise; C's to
# 500 is a short all-in raise; A calls all-in for 100. C's top 200 is
# uncalled (B, the next deepest, put in 300) and comes back as a refund,
# leaving A 100, B 300, C 300 committed. Layers:
#   main  100 x 3            = 300, A B C eligible
#   side (300 - 100) x 2     = 400, B C eligible
SHOVE_CASES = [
    (   # A's aces take the main; B's kings beat C's queens for the side;
        # C keeps only the refund.
        {0: ("As", "Ah"), 1: ("Ks", "Kh"), 2: ("Qs", "Qh")},
        [(300, [0, 1, 2], {0: 300}), (400, [1, 2], {1: 400})],
        [300, 400, 200]),
    (   # C's aces take both pots on top of the refund: 300 + 400 + 200.
        {0: ("Qs", "Qh"), 1: ("Ks", "Kh"), 2: ("As", "Ah")},
        [(300, [0, 1, 2], {2: 300}), (400, [1, 2], {2: 400})],
        [0, 0, 900]),
    (   # B's aces take main and side; C keeps the refund; A busts.
        {0: ("Qs", "Qh"), 1: ("As", "Ah"), 2: ("Ks", "Kh")},
        [(300, [0, 1, 2], {1: 300}), (400, [1, 2], {1: 400})],
        [0, 700, 200]),
]


@pytest.mark.parametrize("case", SHOVE_CASES, ids=[
    "short-main-middle-side-deep-refunded", "deep-stack-wins-everything",
    "middle-wins-main-and-side"])
def test_three_way_all_in_pays_exact_side_pots(case):
    holes, pots, final = case
    stacks, result = play_out([100, 300, 500], holes, BOARD, _shove)
    assert result["refund"] == [2, 200]
    assert pots_of(result) == pots
    assert stacks == final
    assert sum(stacks) == 900


def test_folded_chips_stay_in_the_side_pot():
    """B opens to 250, C shoves 500, A calls all-in for 100, B folds.

    C's raise to 500 was only called to B's 250, so 250 comes back to C,
    leaving A 100, B 250 (folded), C 250 committed. Layers:
      main  100 x 3          = 300, A and C eligible (B folded)
      side  (250 - 100) x 2  = 300, C alone -- B's dead 150 is in it
    A's aces beat C's queens for the main; C takes the side it alone
    contests. Final: A 300, B 300 - 250 = 50, C 250 + 300 = 550."""
    holes = {0: ("As", "Ah"), 1: ("Ks", "Kh"), 2: ("Qs", "Qh")}
    stacks, result = play_out([100, 300, 500], holes, BOARD,
                              _b_raises_then_folds_to_c)
    assert result["refund"] == [2, 250]
    assert pots_of(result) == [(300, [0, 2], {0: 300}),
                               (300, [2], {2: 300})]
    assert stacks == [300, 50, 550]


def test_split_main_pot_gives_the_odd_chip_left_of_the_button():
    """Stacks [101, 300, 500], all shove. A calls all-in for 101 and C's
    top 200 is refunded, so A 101, B 300, C 300 are committed:
      main  101 x 3          = 303, A B C eligible
      side  (300 - 101) x 2  = 398, B C eligible
    The board T J Q K 2 gives A (As 3h) and C (Ad 6h) the same ace-high
    straight; B (4s 5h) has king high. The main splits 151 each with one
    chip over, and the odd chip goes to the first winner left of the
    button (seat 1), which is C, not A. C's straight also wins the side.
    Final: A 151, B 0, C 200 + 152 + 398 = 750."""
    holes = {0: ("As", "3h"), 1: ("4s", "5h"), 2: ("Ad", "6h")}
    stacks, result = play_out([101, 300, 500], holes,
                              ["Tc", "Jd", "Qh", "Ks", "2d"], _shove)
    assert result["refund"] == [2, 200]
    assert pots_of(result) == [(303, [0, 1, 2], {0: 151, 2: 152}),
                               (398, [1, 2], {2: 398})]
    assert stacks == [151, 0, 750]
    assert sum(stacks) == 901


# ------------------------------------------------------------- ordering

def test_out_of_turn_rejected_without_desync():
    reps = make_replicas(2, ["A", "B", "C"], [500, 500, 500])
    wrong = (reps[0].actor + 1) % 3
    d0 = reps[0].state_digest()
    assert apply_all(reps, 0, wrong, "call") == "rejected"
    assert reps[0].state_digest() == d0            # nothing moved
    assert reps[0].next_seq == 0


def test_out_of_order_delivery_buffers_to_total_order():
    """Replica B receives action 1 before action 0; once 0 arrives it must
    end up identical to replica A which saw them in order."""
    ra, rb = make_replicas(2, ["A", "B", "C"], [500, 500, 500])
    first_seat = ra.actor
    # apply action 0 to A only, to learn the follow-up actor
    assert ra.apply_action(0, first_seat, "call") == "applied"
    second_seat = ra.actor
    assert ra.apply_action(1, second_seat, "call") == "applied"
    # B gets them REVERSED
    assert rb.apply_action(1, second_seat, "call") == "buffered"
    assert rb.state_digest() != ra.state_digest()  # not yet
    assert rb.apply_action(0, first_seat, "call") == "applied"  # drains buffer
    assert rb.next_seq == 2
    assert rb.state_digest() == ra.state_digest()  # converged


def test_stale_and_garbage_rejected():
    reps = make_replicas(2, ["A", "B", "C"], [500, 500, 500])
    seat = reps[0].actor
    assert apply_all(reps, 0, seat, "call") == "applied"
    assert apply_all(reps, 0, seat, "call") == "stale"       # duplicate seq
    d = reps[0].state_digest()
    assert apply_all(reps, 1, reps[0].actor, "banana") == "rejected"
    assert reps[0].state_digest() == d


def test_settle_requires_complete_board():
    reps = make_replicas(2, ["A", "B"], [500, 500])
    r = reps[0]
    seq = 0
    # get to an all-in lockup preflop (contested 2, board 0)
    while r.phase == PHASE_BETTING:
        seat = r.actor
        lg = r.engine.legal(seat)
        act = ("raise", lg["max_to"]) if lg["can_raise"] else ("call", 0)
        for rep in reps:
            rep.apply_action(seq, seat, act[0], act[1])
        seq += 1
    assert r.phase == PHASE_STREET_OVER
    with pytest.raises(RuntimeError):
        r.finish()                                  # board incomplete


def test_settle_that_does_not_conserve_chips_is_refused():
    """finish() counts the chips after settling. A chip that appeared from
    nowhere mid-hand (or one an engine defect lost) must not be paid out:
    the hand stays unsettled so the session can void it and redeal from
    the stacks it was dealt with."""
    (r,) = make_replicas(1, ["A", "B", "C"], [500, 500, 500])
    assert r.start_total == 1500
    seq = 0
    while r.phase == PHASE_BETTING:
        assert r.apply_action(seq, r.actor, "fold") == "applied"
        seq += 1
    assert r.phase == PHASE_HAND_OVER
    r.engine.players[0].stack += 1                  # a chip from nowhere
    with pytest.raises(ChipConservationError,
                       match="settled to 1501 chips but was dealt with 1500"):
        r.finish()
    assert r.result is None
    assert r.phase == PHASE_HAND_OVER


# ------------------------------------------------------------- the fuzz

@pytest.mark.parametrize("seed", [7, 1234, 999983])
def test_seeded_fuzz_hands_stay_in_sync(seed):
    """Random legal actions, three replicas, digest compared after EVERY
    step -- the determinism + sync proof."""
    rng = random.Random(seed)
    cards = list(FULL_DECK)
    rng.shuffle(cards)
    board_cards = cards[:5]
    holes = {i: [cards[5 + 2 * i], cards[6 + 2 * i]] for i in range(4)}

    reps = make_replicas(3, ["A", "B", "C", "D"], [300, 220, 500, 90],
                         hand=seed, button=rng.randrange(4))
    assert_synced(reps)
    seq = 0
    streets = [board_cards[0:3], board_cards[3:4], board_cards[4:5]]
    for _ in range(400):
        phase = reps[0].phase
        if phase == PHASE_BETTING:
            seat = reps[0].actor
            lg = reps[0].engine.legal(seat)
            roll = rng.random()
            if lg["can_raise"] and roll < 0.35:
                amount = rng.randint(lg["min_to"], max(lg["min_to"],
                                                       min(lg["max_to"],
                                                           lg["min_to"] + 60)))
                apply_all(reps, seq, seat, "raise", amount)
            elif lg["to_call"] > 0 and roll < 0.55:
                apply_all(reps, seq, seat, "fold")
            else:
                apply_all(reps, seq, seat, "call")
            seq += 1
        elif phase == PHASE_STREET_OVER:
            nxt = streets.pop(0)
            for r in reps:
                r.advance_street(nxt)
            assert_synced(reps)
        elif phase in (PHASE_SHOWDOWN, PHASE_HAND_OVER):
            results = []
            for r in reps:
                if phase == PHASE_SHOWDOWN:
                    r.set_all_holes(holes)
                results.append(r.finish(force_tabled=(phase == PHASE_SHOWDOWN)))
            assert_synced(reps)
            assert all(res == results[0] for res in results)
            return                                   # hand complete, in sync
    pytest.fail("fuzz hand did not terminate")


if __name__ == "__main__":
    passed = total = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        marks = getattr(fn, "pytestmark", [])
        params = None
        for m in marks:
            if m.name == "parametrize":
                params = m.args[1]
        cases = params if params else [None]
        for c in cases:
            total += 1
            try:
                fn(c) if params else fn()
                passed += 1
                print(f"  {name}{'['+str(c)+']' if params else ''}: ok")
            except Exception as exc:
                print(f"  {name}{'['+str(c)+']' if params else ''}: FAIL - {type(exc).__name__}: {exc}")
    print(f"{passed}/{total} passed")
