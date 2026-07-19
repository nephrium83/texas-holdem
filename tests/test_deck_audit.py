"""Pins holdem/p2p/deck_audit.py -- the post-hand full-deck audit.

The properties that matter: an honest hand audits clean; a lying
decryptor is identified BY SEAT; a corrupted deck (duplicate /
substitution / smuggled trivial ciphertext) is detected with certainty
even when every decryptor is honest; and the chain walk attributes a
corrupt deck to the exact shuffler round that introduced it.
"""
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import ristretto as R
    from holdem.p2p import elgamal as eg
    from holdem.p2p import shuffle_mp as sh
    from holdem.p2p import deck_audit as da
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


def _table(n_seats=3, n_shuffles=None):
    """Keys, a fully shuffled deck from the trivial start, and pubkeys."""
    xs = [R.random_scalar() for _ in range(n_seats)]
    Xs = [R.mul_base(x) for x in xs]
    pk = eg.joint_public_key(Xs)
    deck = eg.make_trivial_deck()
    for _ in range(n_shuffles if n_shuffles is not None else n_seats):
        deck, _ = sh.shuffle_deck(pk, deck)
    return xs, Xs, pk, deck


def _all_shares(deck, xs):
    return [da.make_shares(deck, x) for x in xs]


# --------------------------------------------------------------- happy path

def test_honest_hand_audits_clean():
    xs, Xs, pk, deck = _table()
    rep = da.audit_deck(deck, Xs, _all_shares(deck, xs))
    assert rep.ok is True
    assert rep.bad_seats == [] and rep.problems == []
    assert Counter(rep.cards) == Counter(eg.CARDS)


def test_share_is_52_proven_positions():
    xs, Xs, pk, deck = _table(n_seats=2)
    shares = da.make_shares(deck, xs[0])
    assert len(shares) == 52
    assert all(len(ps.proof) == 64 for ps in shares)


# --------------------------------------------------------------- bad decryptor

def test_lying_seat_identified_by_dleq():
    xs, Xs, pk, deck = _table()
    shares = _all_shares(deck, xs)
    liar = R.random_scalar()                      # wrong secret
    shares[1] = da.make_shares(deck, liar)        # honest proofs, wrong key
    rep = da.audit_deck(deck, Xs, shares)
    assert rep.ok is False
    assert rep.bad_seats == [1]
    assert any("seat 1: DLEQ failed" in p for p in rep.problems)


# --------------------------------------------------------------- corrupt deck

def test_duplicate_card_detected_with_honest_decryptors():
    """A shuffler copied one ciphertext over another: every DLEQ passes,
    the multiset check fails, and the duplicate/missing cards are named."""
    xs, Xs, pk, deck = _table()
    deck = list(deck)
    deck[10] = deck[3]                             # duplicate
    shares = _all_shares(deck, xs)                 # honest shares of corrupt deck
    rep = da.audit_deck(deck, Xs, shares)
    assert rep.ok is False
    assert rep.bad_seats == []                     # decryptors are honest
    assert any("duplicated cards:" in p for p in rep.problems)
    assert any("missing cards:" in p for p in rep.problems)


def test_noncard_substitution_detected():
    xs, Xs, pk, deck = _table()
    deck = list(deck)
    stray = R.mul_base(R.random_scalar())          # not a card point
    deck[7] = eg.encrypt(pk, stray)
    shares = _all_shares(deck, xs)
    rep = da.audit_deck(deck, Xs, shares)
    assert rep.ok is False
    assert rep.cards[7] is None
    assert any("position 7: decrypts to a non-card point" in p for p in rep.problems)


def test_smuggled_trivial_ciphertext_flagged():
    xs, Xs, pk, deck = _table()
    deck = list(deck)
    deck[0] = eg.make_trivial_deck()[0]
    with pytest.raises(ValueError):
        da.make_shares(deck, xs[0])                # sharer refuses
    # and the auditor flags it structurally even without shares for it
    rep = da.audit_deck(deck, Xs, [[]] * len(Xs))
    assert rep.ok is False
    assert any("trivial ciphertext" in p for p in rep.problems)


# --------------------------------------------------------------- structure

def test_wrong_deck_size_rejected():
    xs, Xs, pk, deck = _table()
    rep = da.audit_deck(deck[:51], Xs, _all_shares(deck, xs))
    assert rep.ok is False
    assert any("expected 52" in p for p in rep.problems)


def test_short_share_list_attributed():
    xs, Xs, pk, deck = _table()
    shares = _all_shares(deck, xs)
    shares[2] = shares[2][:40]
    rep = da.audit_deck(deck, Xs, shares)
    assert rep.ok is False
    assert 2 in rep.bad_seats


# --------------------------------------------------------------- chain walk

def test_chain_attributes_corrupt_shuffler():
    """Rounds 1..3; shuffler 2 duplicates a card in its OUTPUT. Auditing
    each broadcast deck in order pins the corruption on round 2: round 1
    audits clean, round 2 does not (and 3, shuffling a corrupt deck,
    inherits the corruption)."""
    n = 3
    xs = [R.random_scalar() for _ in range(n)]
    Xs = [R.mul_base(x) for x in xs]
    pk = eg.joint_public_key(Xs)

    decks = []
    deck = eg.make_trivial_deck()                  # round 0: by inspection
    assert eg.verify_trivial_deck(deck)

    deck, _ = sh.shuffle_deck(pk, deck)            # round 1 (honest)
    decks.append(deck)

    deck, _ = sh.shuffle_deck(pk, deck)            # round 2 -- then cheats:
    deck = list(deck)
    deck[20] = deck[4]                             # duplicate a card
    decks.append(deck)

    deck, _ = sh.shuffle_deck(pk, deck)            # round 3 (honest, corrupt input)
    decks.append(deck)

    reports = [da.audit_deck(d, Xs, _all_shares(d, xs)) for d in decks]
    assert reports[0].ok is True
    assert reports[1].ok is False
    assert reports[2].ok is False                  # corruption propagates
    assert da.first_corrupt_round(reports) == 1    # 0-based -> shuffler round 2


def test_first_corrupt_round_none_when_clean():
    xs, Xs, pk, deck = _table()
    rep = da.audit_deck(deck, Xs, _all_shares(deck, xs))
    assert da.first_corrupt_round([rep, rep]) is None


if __name__ == "__main__":
    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  {name}: ok")
            passed += 1
        except Exception as exc:
            print(f"  {name}: FAIL - {type(exc).__name__}: {exc}")
    print(f"{passed}/{len(fns)} passed")
