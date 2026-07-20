"""Pins holdem/p2p/mental_deal.py -- Phase A (distributed key ceremony).

Includes the in-process broadcast simulation harness that later phases
reuse. Key properties: every honest seat computes the SAME joint key;
key shares are deterministic (survive a simulated restart); a bad PoP or
rogue-key seat aborts the ceremony with attribution.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import ristretto as R
    from holdem.p2p import elgamal as eg
    from holdem.p2p import keygen_pop
    from holdem.p2p.mental_deal import MentalDeal, Phase, derive_share
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


# ------------------------------------------------------ simulation harness

def run_broadcast(seats, master_secrets, session="s", hand=1, button=0):
    """Drive a full peer-symmetric exchange to a fixed point.

    Creates one MentalDeal per seat, calls start() on each, then delivers
    every emitted message to every seat (including the emitter) until no
    new messages are produced. Returns the list of MentalDeal instances.

    Messages may be mutated per-seat by a test (to simulate a cheater)
    before delivery via the ``tamper`` hook.
    """
    deals = {
        s: MentalDeal(session_id=session, hand_no=hand, seat=s,
                      seats_in=list(seats), button=button,
                      master_secret=master_secrets[s])
        for s in seats
    }
    # queue of (msg) to broadcast to everyone
    queue = []
    for s in seats:
        queue.extend(deals[s].start())

    while queue:
        msg = queue.pop(0)
        for s in seats:
            out = deals[s].handle(dict(msg))     # copy: seats can't share refs
            queue.extend(out)
    return deals


def _secrets(seats):
    return {s: f"master-secret-of-seat-{s}".encode() for s in seats}


# ------------------------------------------------------ deterministic shares

def test_share_is_deterministic():
    ms = b"the-master-secret"
    a = derive_share(ms, "sess", 7, 3)
    b = derive_share(ms, "sess", 7, 3)
    assert bytes(a) == bytes(b)


def test_share_varies_by_context():
    ms = b"m"
    base = derive_share(ms, "s", 1, 0)
    assert bytes(base) != bytes(derive_share(ms, "s", 1, 1))    # seat
    assert bytes(base) != bytes(derive_share(ms, "s", 2, 0))    # hand
    assert bytes(base) != bytes(derive_share(ms, "t", 1, 0))    # session
    assert bytes(base) != bytes(derive_share(b"n", "s", 1, 0))  # master


# ------------------------------------------------------ happy path

@pytest.mark.parametrize("n", [2, 3, 6, 9])
def test_ceremony_completes_all_seats_agree(n):
    """run_broadcast now drives A+B to completion: keygen agrees on one
    joint key, then the shuffle chain runs to the end (Phase.DEAL)."""
    seats = list(range(n))
    deals = run_broadcast(seats, _secrets(seats))
    for s in seats:
        assert deals[s].is_done_with_keygen()
        assert deals[s].abort_reason is None
        assert deals[s].phase == Phase.DEAL          # shuffle chain finished
        assert deals[s].is_shuffle_complete()
    # all seats computed the identical joint key
    pks = {bytes(deals[s].joint_pk) for s in seats}
    assert len(pks) == 1


def test_joint_key_matches_sum_of_shares():
    seats = [0, 1, 2, 3]
    ms = _secrets(seats)
    deals = run_broadcast(seats, ms)
    # recompute PK independently from the deterministic shares
    Xs = [R.mul_base(derive_share(ms[s], "s", 1, s)) for s in seats]
    expected = eg.joint_public_key(Xs)
    assert bytes(deals[0].joint_pk) == bytes(expected)


def test_restart_regenerates_same_share():
    """A crashed seat that reconstructs its MentalDeal derives the same
    share and produces an identical key_announce -- crash survival."""
    seats = [0, 1, 2]
    ms = _secrets(seats)
    d1 = MentalDeal("s", 1, 1, list(seats), 0, ms[1])
    first = d1.start()
    # simulate crash + reopen: brand-new instance, same inputs
    d2 = MentalDeal("s", 1, 1, list(seats), 0, ms[1])
    second = d2.start()
    assert first[0]["X_hex"] == second[0]["X_hex"]     # same public share


# ------------------------------------------------------ soundness / attribution

def test_bad_pop_aborts_with_attribution():
    """One seat sends a valid X but a PoP for a different key -> abort,
    and every honest seat names that seat."""
    seats = [0, 1, 2]
    ms = _secrets(seats)
    deals = {
        s: MentalDeal("s", 1, s, list(seats), 0, ms[s]) for s in seats
    }
    queue = []
    for s in seats:
        queue.extend(deals[s].start())

    # corrupt seat 2's announce: replace its pop with a pop for a random key
    from holdem.p2p.mental_deal import _pop_ctx
    bogus_x = R.random_scalar()
    bad_pop = keygen_pop.prove(bogus_x, _pop_ctx("s", 1, 2))
    for msg in queue:
        if msg["seat"] == 2:
            msg["pop_hex"] = bad_pop.hex()    # X stays, pop no longer matches

    while queue:
        msg = queue.pop(0)
        for s in seats:
            deals[s].handle(dict(msg))

    for s in (0, 1):
        assert deals[s].phase == Phase.ABORTED
        assert deals[s].bad_seat == 2


def test_rogue_key_share_aborts():
    """The rogue-key attack at the ceremony: seat 2 announces
    X_rogue = X_target - sum(others) with a pop it cannot validly make ->
    the ceremony aborts (no valid pop exists for the rogue share)."""
    seats = [0, 1, 2]
    ms = _secrets(seats)
    deals = {s: MentalDeal("s", 1, s, list(seats), 0, ms[s]) for s in seats}
    queue = []
    for s in seats:
        queue.extend(deals[s].start())

    # build the rogue share from the honest announces already in the queue
    from holdem.p2p.mental_deal import _pop_ctx
    honest = {m["seat"]: R.point_from_bytes(bytes.fromhex(m["X_hex"]))
              for m in queue if m["seat"] in (0, 1)}
    x_star = R.random_scalar()
    X_target = R.mul_base(x_star)
    X_rogue = R.sub(X_target, R.add(honest[0], honest[1]))
    # attacker's best attempt: prove for x_star (the log it knows)
    forged = keygen_pop.prove(x_star, _pop_ctx("s", 1, 2))
    for msg in queue:
        if msg["seat"] == 2:
            msg["X_hex"] = bytes(X_rogue).hex()
            msg["pop_hex"] = forged.hex()

    while queue:
        msg = queue.pop(0)
        for s in seats:
            deals[s].handle(dict(msg))

    for s in (0, 1):
        assert deals[s].phase == Phase.ABORTED
        assert deals[s].bad_seat == 2


def test_unknown_seat_aborts():
    seats = [0, 1]
    ms = _secrets(seats)
    d = MentalDeal("s", 1, 0, list(seats), 0, ms[0])
    d.start()
    d.handle({"type": "key_announce", "seat": 5,
                    "X_hex": bytes(R.mul_base(R.random_scalar())).hex(),
                    "pop_hex": keygen_pop.prove(R.random_scalar(), b"x").hex()})
    assert d.phase == Phase.ABORTED
    assert d.bad_seat == 5


def test_malformed_announce_aborts():
    seats = [0, 1]
    ms = _secrets(seats)
    d = MentalDeal("s", 1, 0, list(seats), 0, ms[0])
    d.start()
    d.handle({"type": "key_announce", "seat": 1, "X_hex": "not-hex", "pop_hex": "00"})
    assert d.phase == Phase.ABORTED


def test_conflicting_shares_from_one_seat_aborts():
    """A seat announcing two different valid shares before the ceremony
    completes is a fault. Use 3 seats so keygen is still open when the
    second (conflicting) announce for seat 1 arrives (seat 2 has not yet
    announced, so PK is not yet formed)."""
    seats = [0, 1, 2]
    ms = _secrets(seats)
    d = MentalDeal("s", 1, 0, list(seats), 0, ms[0])
    d.start()
    from holdem.p2p.mental_deal import _pop_ctx
    x1 = R.random_scalar()
    X1 = R.mul_base(x1)
    d.handle({"type": "key_announce", "seat": 1, "X_hex": bytes(X1).hex(),
              "pop_hex": keygen_pop.prove(x1, _pop_ctx("s", 1, 1)).hex()})
    assert d.phase == Phase.KEYGEN            # still open: seat 2 pending
    # a DIFFERENT valid share for seat 1 -> conflict
    x1b = R.random_scalar()
    X1b = R.mul_base(x1b)
    d.handle({"type": "key_announce", "seat": 1, "X_hex": bytes(X1b).hex(),
              "pop_hex": keygen_pop.prove(x1b, _pop_ctx("s", 1, 1)).hex()})
    assert d.phase == Phase.ABORTED
    assert d.bad_seat == 1


def test_seat_not_in_seats_in_raises():
    with pytest.raises(ValueError):
        MentalDeal("s", 1, 9, [0, 1, 2], 0, b"m")


# ------------------------------------------------------ Phase B: shuffle chain

def _one_seat_in_shuffle(seat, seats, ms, session="s", hand=1, button=0):
    """Drive a single MentalDeal to the SHUFFLE phase (round 0) by feeding
    it valid key_announces from every other seat."""
    from holdem.p2p.mental_deal import _pop_ctx
    d = MentalDeal(session, hand, seat, list(seats), button, ms[seat])
    d.start()
    for s in seats:
        if s == seat:
            continue
        xs = derive_share(ms[s], session, hand, s)
        X = R.mul_base(xs)
        pop = keygen_pop.prove(xs, _pop_ctx(session, hand, s))
        d.handle({"type": "key_announce", "seat": s, "X_hex": bytes(X).hex(),
                  "pop_hex": pop.hex()})
    return d


def _decrypt_final(deal, seats, ms, session="s", hand=1):
    """Cooperatively decrypt a completed deal's final deck to card labels."""
    xs = {s: derive_share(ms[s], session, hand, s) for s in seats}
    cards = []
    for ct in deal.deck:
        shares = [eg.partial_decrypt(ct, xs[s]) for s in seats]
        cards.append(eg.point_to_card(eg.combine(ct, shares)))
    return cards


@pytest.mark.parametrize("n", [2, 3, 6, 9])
def test_shuffle_chain_final_deck_decrypts_to_52(n):
    """The real end-to-end: A+B produce a deck that cooperatively decrypts
    to exactly the 52 canonical cards."""
    from collections import Counter
    seats = list(range(n))
    ms = _secrets(seats)
    deals = run_broadcast(seats, ms)
    cards = _decrypt_final(deals[0], seats, ms)
    assert Counter(cards) == Counter(eg.CARDS)
    assert len(set(cards)) == 52


def test_all_seats_hold_same_final_deck():
    seats = [0, 1, 2, 3]
    ms = _secrets(seats)
    deals = run_broadcast(seats, ms)
    ref = [ct.to_hex() for ct in deals[0].deck]
    for s in seats:
        assert [ct.to_hex() for ct in deals[s].deck] == ref


def test_every_seat_shuffled_once():
    """After the chain, exactly len(seats) rounds were applied."""
    seats = [0, 1, 2, 3, 4]
    deals = run_broadcast(seats, _secrets(seats))
    for s in seats:
        assert deals[s]._shuffle_round == len(seats)


def test_out_of_turn_shuffle_aborts():
    """A valid deck from the wrong shuffler for the round -> abort +
    attribution."""
    seats = [0, 1, 2]
    ms = _secrets(seats)
    observer = _one_seat_in_shuffle(2, seats, ms)     # round-1 shuffler is seat 0
    from holdem.p2p import shuffle_mp
    deck, _ = shuffle_mp.shuffle_deck(observer.joint_pk, eg.make_trivial_deck())
    # seat 1 (not the round-1 shuffler) broadcasts round 1
    observer.handle({"type": "deck_round", "round": 1, "seat": 1,
                     "deck": [ct.to_hex() for ct in deck]})
    assert observer.phase == Phase.ABORTED
    assert observer.bad_seat == 1


def test_short_deck_aborts():
    seats = [0, 1, 2]
    ms = _secrets(seats)
    observer = _one_seat_in_shuffle(2, seats, ms)
    from holdem.p2p import shuffle_mp
    deck, _ = shuffle_mp.shuffle_deck(observer.joint_pk, eg.make_trivial_deck())
    observer.handle({"type": "deck_round", "round": 1, "seat": 0,
                     "deck": [ct.to_hex() for ct in deck[:51]]})
    assert observer.phase == Phase.ABORTED
    assert observer.bad_seat == 0


def test_trivial_ciphertext_in_shuffle_aborts():
    """A deck carrying a trivial (identity-C0) ciphertext post-shuffle is
    rejected -- a genuine shuffle re-encrypts every card."""
    seats = [0, 1, 2]
    ms = _secrets(seats)
    observer = _one_seat_in_shuffle(2, seats, ms)
    from holdem.p2p import shuffle_mp
    deck, _ = shuffle_mp.shuffle_deck(observer.joint_pk, eg.make_trivial_deck())
    deck = list(deck)
    deck[10] = eg.make_trivial_deck()[10]             # smuggle a trivial ct
    observer.handle({"type": "deck_round", "round": 1, "seat": 0,
                     "deck": [ct.to_hex() for ct in deck]})
    assert observer.phase == Phase.ABORTED
    assert observer.bad_seat == 0


def test_malformed_deck_aborts():
    seats = [0, 1, 2]
    ms = _secrets(seats)
    observer = _one_seat_in_shuffle(2, seats, ms)
    observer.handle({"type": "deck_round", "round": 1, "seat": 0,
                     "deck": [["not-hex", "also-not"]] * 52})
    assert observer.phase == Phase.ABORTED


def test_stale_round_ignored_not_aborted():
    """A duplicate/old round (round <= accepted) is ignored, not an abort."""
    seats = [0, 1, 2]
    ms = _secrets(seats)
    observer = _one_seat_in_shuffle(2, seats, ms)
    from holdem.p2p import shuffle_mp
    # accept a valid round 1 first
    deck1, _ = shuffle_mp.shuffle_deck(observer.joint_pk, eg.make_trivial_deck())
    observer.handle({"type": "deck_round", "round": 1, "seat": 0,
                     "deck": [ct.to_hex() for ct in deck1]})
    assert observer._shuffle_round == 1 and observer.phase == Phase.SHUFFLE
    # re-send round 1 -> ignored, still round 1, not aborted
    observer.handle({"type": "deck_round", "round": 1, "seat": 0,
                     "deck": [ct.to_hex() for ct in deck1]})
    assert observer.phase == Phase.SHUFFLE
    assert observer._shuffle_round == 1


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
                print(f"  {name}{'['+str(c)+']' if params else ''}: FAIL - {exc}")
    print(f"{passed}/{total} passed")
