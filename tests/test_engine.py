"""Engine test suite.

Runs under pytest (CI) or directly: `python tests/test_engine.py`.

Covers:
- evaluator cross-checked against an independent brute-force oracle
- known-hand spot checks and Chen preflop scores
- deterministic side-pot accounting (amounts, eligibility, refunds)
- regression tests for the all-in under-raise rule
- full-game fuzzing: chip conservation, action legality, and the
  round-close invariant (every live bet matched when a street ends)
"""
import random
import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.engine import (Card, Engine, Player, Brain, evaluate, chen,
                           FULL_DECK, AI_STYLES)


# ------------------------------------------------------------------ oracle

def ref_eval5(hand):
    """Independent 5-card evaluator used purely as a reference."""
    vals = [c.v for c in hand]
    suits = [c.s for c in hand]
    counts = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    is_flush = len(set(suits)) == 1
    uniq = sorted(set(vals))
    is_straight = False
    high = None
    if len(uniq) == 5 and uniq[-1] - uniq[0] == 4:
        is_straight, high = True, uniq[-1]
    if set(vals) == {14, 2, 3, 4, 5}:
        is_straight, high = True, 5
    cv = sorted(counts.values(), reverse=True)
    if is_flush and is_straight:
        return 8, (high,)
    if 4 in cv:
        q = [v for v, c in counts.items() if c == 4][0]
        return 7, (q, max(v for v in vals if v != q))
    if 3 in cv and 2 in cv:
        t = max(v for v, c in counts.items() if c == 3)
        p = max(v for v, c in counts.items() if c == 2)
        return 6, (t, p)
    if is_flush:
        return 5, tuple(sorted(vals, reverse=True))
    if is_straight:
        return 4, (high,)
    if 3 in cv:
        t = max(v for v, c in counts.items() if c == 3)
        k = sorted([v for v in vals if v != t], reverse=True)[:2]
        return 3, (t, *k)
    if cv.count(2) == 2:
        ps = sorted([v for v, c in counts.items() if c == 2], reverse=True)
        return 2, (*ps, max(v for v in vals if v not in ps))
    if 2 in cv:
        p = max(v for v, c in counts.items() if c == 2)
        k = sorted([v for v in vals if v != p], reverse=True)[:3]
        return 1, (p, *k)
    return 0, tuple(sorted(vals, reverse=True))


def ref_best7(cards):
    return max(ref_eval5(c) for c in combinations(cards, 5))


def C(spec):
    """'As' -> Card. Ranks 2-9,T,J,Q,K,A; suits c,d,h,s."""
    r = {"T": 10, "J": 11, "Q": 12, "K": 13, "A": 14}.get(
        spec[0], None) or int(spec[0])
    return Card(r, "cdhs".index(spec[1]))


# ------------------------------------------------------------------- tests

def test_evaluator_vs_oracle():
    rng = random.Random(7)
    for _ in range(20000):
        cards = rng.sample(FULL_DECK, 7)
        assert evaluate(cards) == ref_best7(cards), \
            f"mismatch on {[str(c) for c in cards]}"


def test_known_hands():
    cases = [
        ("As Ks Qs Js Ts 2c 3d", (8, (14,))),            # royal
        ("5h 4h 3h 2h Ah Kc Kd", (8, (5,))),             # wheel SF
        ("9c 9d 9h 9s Ac 2c 3c", (7, (9, 14))),          # quads
        ("Ac Ad Ah Kc Kd 2s 3s", (6, (14, 13))),         # full house
        ("Ac Ad Ah Kc Kd Ks 2s", (6, (14, 13))),         # two trips -> boat
        ("Ah Kh 9h 5h 2h 3c 4c", (5, (14, 13, 9, 5, 2))),
        ("Ah Kc Qd Js Tc 2c 2d", (4, (14,))),            # broadway
        ("Ah 2c 3d 4s 5c 9c 9d", (4, (5,))),             # wheel
        ("9c 9d 9h Ac Kc 2s 3s", (3, (9, 14, 13))),
        ("Ac Ad Kc Kd 9s 2s 3s", (2, (14, 13, 9))),
        ("Ac Ad Kc Qd 9s 2s 3s", (1, (14, 13, 12, 9))),
        ("Ac Kd Qc Js 9s 2s 3s", (0, (14, 13, 12, 11, 9))),
    ]
    for spec, want in cases:
        cards = [C(x) for x in spec.split()]
        assert evaluate(cards) == want, spec


def test_chen():
    cases = [("Ac Ad", 20), ("Kc Kd", 16), ("As Ks", 12), ("Ac Kd", 10),
             ("Js Ts", 9), ("7c 2d", -1), ("5s 4s", 6)]
    for spec, want in cases:
        assert chen([C(x) for x in spec.split()]) == want, spec


def _run_hand(e, brain):
    """Play one already-started hand to completion; assert invariants."""
    while True:
        if len(e.contested()) <= 1 or e.street == "showdown":
            break
        if e.actor is None:
            for p in e.contested():
                if not p.all_in:
                    assert p.bet == e.current_bet, (
                        f"round closed with {p.name} at {p.bet} "
                        f"vs current_bet {e.current_bet}")
            e.next_street()
            continue
        i = e.actor
        a, m = brain.decide(e, i)
        if a == "raise":
            assert e.legal(i)["can_raise"], f"illegal raise by seat {i}"
        e.act(i, a, m)
    return e.settle()


def test_game_fuzz():
    """Chip conservation + legality + round-close invariant, all structures."""
    for seed in range(30):
        for n in (2, 3, 6, 9):
            for structure in ("No-Limit", "Pot-Limit", "Fixed-Limit"):
                rng = random.Random(seed * 100 + n)
                stack = rng.choice([300, 1000])
                ps = [Player(i, f"P{i}", stack,
                             style=rng.choice(AI_STYLES),
                             level=rng.randint(1, 3)) for i in range(n)]
                e = Engine(ps, 10, 20, structure, rng)
                brain = Brain(rng)
                bank = stack * n
                for _ in range(12):
                    if sum(1 for p in ps if p.stack > 0) < 2:
                        break
                    if not e.start_hand():
                        break
                    _run_hand(e, brain)
                    e.drain()
                    assert sum(p.stack for p in ps) == bank, "chip leak"
                    assert not any(p.stack < 0 for p in ps)


def test_side_pot_accounting():
    """Three-way all-in: exact pot layering, eligibility, and refund."""
    ps = [Player(0, "Short", 50), Player(1, "Mid", 120), Player(2, "Big", 300)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(3))
    e.button = 2                       # -> btn 0, sb 1, bb 2, first actor 0
    assert e.start_hand()
    e.act(0, "raise", 50)              # short jams 50
    e.act(1, "raise", 120)             # mid jams 120
    e.act(2, "raise", 300)             # big jams 300
    res = e.settle()

    assert res["refund"] == (2, 180)               # 300-120 uncalled
    amounts = [(p["amount"], sorted(p["eligible"])) for p in res["pots"]]
    assert amounts == [(150, [0, 1, 2]),           # 50*3 main
                       (140, [1, 2])]              # 70*2 side
    assert sum(p.stack for p in ps) == 470         # bank conserved


def test_underraise_reopens_calls_only():
    """Regression: after an all-in under-raise, earlier actors must be asked
    to call the difference but may not re-raise."""
    ps = [Player(0, "P0", 1000), Player(1, "P1", 95), Player(2, "P2", 1000)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(1))
    e.button = 2
    e.start_hand()
    e.act(0, "raise", 60)              # full raise, min_raise=40
    e.act(1, "raise", 95)              # +35 all-in: under-raise
    e.act(2, "fold")
    assert e.actor == 0, "action must return to the original raiser"
    lg = e.legal(0)
    assert lg["to_call"] == 35
    assert not lg["can_raise"]
    e.act(0, "call")
    assert e.actor is None
    for p in e.contested():
        if not p.all_in:
            assert p.bet == e.current_bet


def test_full_raise_restores_raise_rights():
    ps = [Player(0, "P0", 1000), Player(1, "P1", 95),
          Player(2, "P2", 1000), Player(3, "P3", 1000)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(1))
    e.button = 3                       # btn 0, sb 1, bb 2, first actor 3
    e.start_hand()
    e.act(3, "fold")
    e.act(0, "raise", 60)
    e.act(1, "raise", 95)              # under-raise jam
    assert 0 in e.no_raise
    e.act(2, "raise", 200)             # full raise re-opens betting
    assert 0 not in e.no_raise
    assert e.legal(0)["can_raise"]


def test_heads_up_positions():
    """HU: the button posts the small blind and acts first preflop."""
    ps = [Player(0, "A", 500), Player(1, "B", 500)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(2))
    e.start_hand()
    assert e.sb_i == e.button
    assert e.actor == e.button


if __name__ == "__main__":
    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for name, fn in fns:
        fn()
        print(f"  {name}: ok")
    print(f"ALL PASS ({len(fns)} tests)")
