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




def _play_out(e, brain):
    while True:
        if len(e.contested()) <= 1 or e.street == "showdown":
            break
        if e.actor is None:
            e.next_street()
            continue
        a, m = brain.decide(e, e.actor)
        e.act(e.actor, a, m)
    return e.settle()


def test_dead_button_sb_busts():
    """SB busts -> next hand the button sits on the vacated seat and the
    old BB posts the SB; nobody skips a blind."""
    ps = [Player(i, f"P{i}", 1000) for i in range(4)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(5))
    e.button = 3
    e.start_hand()                       # btn 0, sb 1, bb 2
    assert (e.button, e.sb_seat, e.bb_seat) == (0, 1, 2)
    e.settle()
    ps[1].stack = 0                      # the SB busts
    e.start_hand()
    assert e.bb_seat == 3                # BB advanced one seat
    assert e.sb_seat == 2 and e.sb_i == 2   # old BB posts SB
    assert e.button == 1                 # dead button on the vacated seat
    assert not ps[1].in_seat


def test_dead_small_blind_bb_busts():
    """BB busts -> next hand has a dead small blind (nobody posts it)."""
    ps = [Player(i, f"P{i}", 1000) for i in range(4)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(5))
    e.button = 3
    e.start_hand()                       # btn 0, sb 1, bb 2
    e.settle()
    ps[2].stack = 0                      # the BB busts
    e.start_hand()
    assert e.bb_seat == 3
    assert e.sb_seat == 2 and e.sb_i is None    # dead SB, not posted
    assert e.button == 1
    assert sum(p.total for p in ps) == e.bb     # only the BB is in


def test_bb_never_posts_twice_in_a_row():
    """Fuzz busts between hands; the BB anchor must advance every hand."""
    rng = random.Random(11)
    for seed in range(20):
        ps = [Player(i, f"P{i}", 1000) for i in range(6)]
        e = Engine(ps, 10, 20, "No-Limit", random.Random(seed))
        brain = Brain(random.Random(seed))
        last_bb = None
        for _ in range(15):
            live = [p for p in ps if p.stack > 0]
            if len(live) > 2 and rng.random() < 0.25:
                rng.choice(live).stack = 0          # simulate a bust
            if sum(1 for p in ps if p.stack > 0) < 2:
                break
            if not e.start_hand():
                break
            if last_bb is not None and sum(1 for p in ps if p.in_seat) >= 3:
                assert e.bb_seat != last_bb, "same seat posted BB twice"
            last_bb = e.bb_seat
            _play_out(e, brain)


def test_bb_ante_math():
    """BB posts blind first then ante; ante is dead (never refunded) but
    plays in the pot; walks still net the BB only the SB."""
    ps = [Player(i, f"P{i}", 1000) for i in range(3)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(2), bb_ante=True)
    e.button = 2
    e.start_hand()                       # btn 0, sb 1, bb 2
    bb = ps[e.bb_seat]
    assert bb.total_live == 20 and bb.total_dead == 20
    # everyone folds to the BB: walk
    e.act(0, "fold")
    e.act(1, "fold")
    res = e.settle()
    assert sum(p.stack for p in ps) == 3000
    assert bb.stack == 1010              # wins the SB, ante comes home in pot
    assert res["refund"] == (bb.idx, 10) # live refund only (BB bet vs SB 10)


def test_bb_ante_short_stack():
    """Blind before ante when the stack can't cover both."""
    ps = [Player(0, "A", 1000), Player(1, "B", 1000), Player(2, "C", 25)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(2), bb_ante=True)
    e.button = 2                         # bb lands on seat 2 (25 chips)
    e.start_hand()
    c = ps[2]
    assert c.total_live == 20 and c.total_dead == 5 and c.all_in


def test_straddle():
    ps = [Player(i, f"P{i}", 1000) for i in range(4)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(3))
    e.button = 3
    e.start_hand(straddle_fn=lambda utg: True)
    assert e.straddler == 3              # utg after bb seat 2
    assert e.current_bet == 40
    assert e.actor == 0                  # action starts left of the straddle
    lg = e.legal(0)
    assert lg["to_call"] == 40 and lg["min_to"] == 80
    # fold to the straddler: they must get their option
    e.act(0, "fold")
    e.act(1, "call")                     # sb calls 30 more
    e.act(2, "call")                     # bb calls 20 more
    assert e.actor == 3                  # straddler's option
    assert e.legal(3)["can_check"]


def test_no_straddle_heads_up_or_limit():
    ps = [Player(i, f"P{i}", 1000) for i in range(2)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(3))
    e.start_hand(straddle_fn=lambda utg: True)
    assert e.straddler is None
    ps = [Player(i, f"P{i}", 1000) for i in range(4)]
    e = Engine(ps, 10, 20, "Fixed-Limit", random.Random(3))
    e.start_hand(straddle_fn=lambda utg: True)
    assert e.straddler is None


def test_run_it_twice():
    """Two runs share no cards, each awards half the pot, chips conserved."""
    ps = [Player(0, "A", 500), Player(1, "B", 500), Player(2, "C", 500)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(9))
    e.button = 2
    e.start_hand()
    e.act(0, "raise", 500)
    e.act(1, "raise", 500)
    e.act(2, "fold")
    assert e.betting_locked()
    res = e.settle(runs=2)
    assert e.board2 is not None
    assert len(e.board) == 5 and len(e.board2) == 5
    ids = {(c.v, c.s) for c in e.board} | {(c.v, c.s) for c in e.board2}
    assert len(ids) == 10 or len(e.board) + len(e.board2) - len(ids) == 0
    assert len(res["runs"]) == 2
    main = res["pots"][0]
    assert sum(r["amount"] for r in main["runs"]) == main["amount"]
    assert sum(p.stack for p in ps) == 1500


def test_run_it_twice_conservation_fuzz():
    for seed in range(25):
        rng = random.Random(seed)
        ps = [Player(i, f"P{i}", 300, style=rng.choice(AI_STYLES))
              for i in range(4)]
        e = Engine(ps, 10, 20, "No-Limit", rng, bb_ante=True)
        brain = Brain(rng)
        for _ in range(10):
            if sum(1 for p in ps if p.stack > 0) < 2:
                break
            if not e.start_hand():
                break
            while True:
                if len(e.contested()) <= 1 or e.street == "showdown":
                    break
                if e.actor is None:
                    if e.betting_locked() and len(e.board) < 5:
                        break            # settle with two runs
                    e.next_street()
                    continue
                a, m = brain.decide(e, e.actor)
                e.act(e.actor, a, m)
            runs = 2 if (e.betting_locked() and len(e.board) < 5) else 1
            e.settle(runs=runs)
            e.drain()
            assert sum(p.stack for p in ps) == 1200, f"seed {seed}"


def test_showdown_order_and_muck():
    """River aggressor shows first; beaten non-winners muck."""
    ps = [Player(i, f"P{i}", 1000) for i in range(3)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(4))
    e.button = 2                         # btn 0, sb 1, bb 2, first actor 0
    e.start_hand()
    e.act(0, "call"); e.act(1, "call"); e.act(2, "call")
    e.next_street(); e.act(e.actor, "call"); e.act(e.actor, "call"); e.act(e.actor, "call")
    e.next_street(); e.act(e.actor, "call"); e.act(e.actor, "call"); e.act(e.actor, "call")
    e.next_street()                      # river
    first = e.actor
    e.act(first, "raise", 60)            # river bet
    aggr = first
    while e.actor is not None:
        e.act(e.actor, "call")
    res = e.settle()
    assert res["order"][0] == aggr
    assert res["winners"] <= res["shown"]
    for j in res["mucked"]:
        assert j not in res["winners"]
        sc = res["runs"][0]["scores"]
        assert any(sc[k] > sc[j] for k in res["shown"])
    assert res["shown"] | res["mucked"] == set(res["order"])


def test_all_in_hands_are_tabled():
    ps = [Player(0, "A", 300), Player(1, "B", 300)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(6))
    e.start_hand()
    e.act(e.actor, "raise", 300)
    e.act(e.actor, "call")
    res = e.settle()
    assert res["tabled"]
    assert res["mucked"] == set()
    assert res["shown"] == {0, 1}


def test_sitting_out_owes_blinds():
    """A cash player sitting out through their blinds owes them on return."""
    ps = [Player(i, f"P{i}", 1000) for i in range(4)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(8))
    e.button = 3
    brain = Brain(random.Random(8))
    e.start_hand()                       # btn 0 sb 1 bb 2
    _play_out(e, brain)
    e.sit_out(3)                         # seat 3 is next BB
    e.start_hand()                       # BB skips seat 3 -> lands on 0
    assert e.bb_seat == 0
    assert not ps[3].in_seat and ps[3].owes_bb
    _play_out(e, brain)
    e.sit_in(3, post_now=True)
    bank = sum(p.stack for p in ps)
    e.start_hand()
    assert ps[3].in_seat
    assert ps[3].total_live == e.bb      # posted a live BB from position
    _play_out(e, brain)
    assert sum(p.stack for p in ps) == bank


def test_wait_for_bb():
    ps = [Player(i, f"P{i}", 1000) for i in range(4)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(8))
    e.button = 3
    brain = Brain(random.Random(8))
    e.start_hand(); _play_out(e, brain)          # bb seat 2
    e.sit_out(0)
    e.start_hand(); _play_out(e, brain)          # bb seat 3
    e.start_hand(); _play_out(e, brain)          # bb skips 0 -> seat 1
    assert e.bb_seat == 1 and ps[0].owes_bb      # blind passed them
    e.sit_in(0, post_now=False)                  # wait for the natural BB
    assert ps[0].wait_for_bb
    e.start_hand()                               # bb seat 2; 0 still out
    assert not ps[0].in_seat
    _play_out(e, brain)
    e.start_hand(); _play_out(e, brain)          # bb seat 3
    e.start_hand()                               # bb reaches seat 0
    assert e.bb_seat == 0 and ps[0].in_seat
    assert not ps[0].owes_bb and not ps[0].wait_for_bb
    _play_out(e, brain)


def test_add_chips_only_between_hands():
    ps = [Player(i, f"P{i}", 1000) for i in range(3)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(1))
    assert e.add_chips(0, 500)
    assert ps[0].stack == 1500
    e.start_hand()
    assert not e.add_chips(0, 500)       # rejected mid-hand
    assert ps[0].stack in (1500, 1490, 1480)     # only blinds moved it


def test_rabbit_peek_matches_deal():
    ps = [Player(i, f"P{i}", 1000) for i in range(3)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(12))
    e.button = 2
    e.start_hand()
    peek = e.peek_runout()
    assert len(peek) == 5
    before = list(e.deck.cards)
    peek2 = e.peek_runout()
    assert e.deck.cards == before        # non-mutating
    assert [(c.v, c.s) for c in peek] == [(c.v, c.s) for c in peek2]
    e.next_street()
    assert [(c.v, c.s) for c in e.board] == [(c.v, c.s) for c in peek[:3]]
    e.next_street()
    assert (e.board[3].v, e.board[3].s) == (peek[3].v, peek[3].s)
    e.next_street()
    assert (e.board[4].v, e.board[4].s) == (peek[4].v, peek[4].s)


if __name__ == "__main__":
    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for name, fn in fns:
        fn()
        print(f"  {name}: ok")
    print(f"ALL PASS ({len(fns)} tests)")
