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
    ReplicaTable, PHASE_BETTING, PHASE_STREET_OVER, PHASE_SHOWDOWN,
    PHASE_HAND_OVER, PHASE_SETTLED)


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
