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

from crypto_gate import initialize_for_tests, NativeCryptoUnavailable

try:
    initialize_for_tests()
    from holdem.p2p import ristretto as R
    from holdem.p2p import elgamal as eg
    from holdem.p2p import shuffle_mp as sh
    from holdem.p2p import deck_audit as da
except NativeCryptoUnavailable as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


def _table(n_seats=3, n_shuffles=None):
    """Keys, a fully shuffled deck from the trivial start, pubkeys, and the
    shuffle chain (with opened witnesses) that produced the deck."""
    xs = [R.random_scalar() for _ in range(n_seats)]
    Xs = [R.mul_base(x) for x in xs]
    pk = eg.joint_public_key(Xs)
    deck = eg.make_trivial_deck()
    rounds = []
    for r in range(n_shuffles if n_shuffles is not None else n_seats):
        deck, wit = sh.shuffle_deck(pk, deck)
        rounds.append(da.ShuffleRound(seat=r % n_seats, output=deck, witness=wit))
    return xs, Xs, pk, deck, da.ShuffleChain(pk=pk, rounds=rounds)


def _all_shares(deck, xs):
    return [da.make_shares(deck, x) for x in xs]


# --------------------------------------------------------------- happy path

def test_honest_hand_audits_clean():
    xs, Xs, pk, deck, chain = _table()
    rep = da.audit_deck(deck, Xs, _all_shares(deck, xs), chain=chain)
    assert rep.ok is True
    assert rep.bad_seats == [] and rep.problems == []
    assert Counter(rep.cards) == Counter(eg.CARDS)


def test_share_is_52_proven_positions():
    xs, Xs, pk, deck, chain = _table(n_seats=2)
    shares = da.make_shares(deck, xs[0])
    assert len(shares) == 52
    assert all(len(ps.proof) == 64 for ps in shares)


# --------------------------------------------------------------- bad decryptor

def test_lying_seat_identified_by_dleq():
    xs, Xs, pk, deck, chain = _table()
    shares = _all_shares(deck, xs)
    liar = R.random_scalar()                      # wrong secret
    shares[1] = da.make_shares(deck, liar)        # honest proofs, wrong key
    rep = da.audit_deck(deck, Xs, shares, chain=chain)
    assert rep.ok is False
    assert rep.bad_seats == [1]
    assert any("seat 1: DLEQ failed" in p for p in rep.problems)


# --------------------------------------------------------------- corrupt deck

def test_duplicate_card_detected_with_honest_decryptors():
    """A shuffler copied one ciphertext over another: every DLEQ passes,
    the multiset check fails, and the duplicate/missing cards are named."""
    xs, Xs, pk, deck, chain = _table()
    deck = list(deck)
    deck[10] = deck[3]                             # duplicate
    shares = _all_shares(deck, xs)                 # honest shares of corrupt deck
    rep = da.audit_deck(deck, Xs, shares, chain=chain)
    assert rep.ok is False
    assert rep.bad_seats == []                     # decryptors are honest
    assert any("duplicated cards:" in p for p in rep.problems)
    assert any("missing cards:" in p for p in rep.problems)


def test_noncard_substitution_detected():
    xs, Xs, pk, deck, chain = _table()
    deck = list(deck)
    stray = R.mul_base(R.random_scalar())          # not a card point
    deck[7] = eg.encrypt(pk, stray)
    shares = _all_shares(deck, xs)
    rep = da.audit_deck(deck, Xs, shares, chain=chain)
    assert rep.ok is False
    assert rep.cards[7] is None
    assert any("position 7: decrypts to a non-card point" in p for p in rep.problems)


def test_smuggled_trivial_ciphertext_flagged():
    xs, Xs, pk, deck, chain = _table()
    deck = list(deck)
    deck[0] = eg.make_trivial_deck()[0]
    with pytest.raises(ValueError):
        da.make_shares(deck, xs[0])                # sharer refuses
    # and the auditor flags it structurally even without shares for it
    rep = da.audit_deck(deck, Xs, [[]] * len(Xs), chain=chain)
    assert rep.ok is False
    assert any("trivial ciphertext" in p for p in rep.problems)


# --------------------------------------------------------------- structure

def test_wrong_deck_size_rejected():
    xs, Xs, pk, deck, chain = _table()
    rep = da.audit_deck(deck[:51], Xs, _all_shares(deck, xs), chain=chain)
    assert rep.ok is False
    assert any("expected 52" in p for p in rep.problems)


def test_short_share_list_attributed():
    xs, Xs, pk, deck, chain = _table()
    shares = _all_shares(deck, xs)
    shares[2] = shares[2][:40]
    rep = da.audit_deck(deck, Xs, shares, chain=chain)
    assert rep.ok is False
    assert 2 in rep.bad_seats


# --------------------------------------------------------------- chain walk

def _keys(n=3):
    xs = [R.random_scalar() for _ in range(n)]
    Xs = [R.mul_base(x) for x in xs]
    return xs, Xs, eg.joint_public_key(Xs)


def test_chain_attributes_corrupt_shuffler():
    """Rounds 1..3; shuffler 1 (round 2) duplicates a card in its OUTPUT.
    Its opened witness cannot reproduce that output, so the chain pins the
    corruption on it -- even though round 3, an honest shuffle of the
    corrupt deck, has a perfectly valid witness."""
    xs, Xs, pk = _keys()
    deck = eg.make_trivial_deck()                  # round 0: by inspection
    assert eg.verify_trivial_deck(deck)
    rounds = []

    deck, w = sh.shuffle_deck(pk, deck)            # round 1 (honest)
    rounds.append(da.ShuffleRound(seat=0, output=deck, witness=w))

    deck, w = sh.shuffle_deck(pk, deck)            # round 2 -- then cheats:
    deck = list(deck)
    deck[20] = deck[4]                             # duplicate a card
    rounds.append(da.ShuffleRound(seat=1, output=deck, witness=w))

    deck, w = sh.shuffle_deck(pk, deck)            # round 3 (honest, corrupt input)
    rounds.append(da.ShuffleRound(seat=2, output=deck, witness=w))

    chain = da.ShuffleChain(pk=pk, rounds=rounds)
    rep = da.audit_deck(deck, Xs, _all_shares(deck, xs), chain=chain)
    assert rep.ok is False
    assert rep.bad_seats == [1]
    assert any("round 2 (seat 1)" in p for p in rep.problems)
    assert any("duplicated cards:" in p for p in rep.problems)


# --------------------------------------------------------------- chain binding
# Audit finding 2: the multiset check alone accepted ANY legal deck.

def test_replacement_deck_with_known_order_is_rejected_and_attributed():
    """THE attack: the last shuffler discards its input and broadcasts a
    fresh encryption of the 52 cards in an order it chose -- a legal deck
    (every DLEQ and the multiset check pass) whose author knows every hole
    card. It cannot produce a witness linking that deck to its input."""
    xs, Xs, pk = _keys()
    deck = eg.make_trivial_deck()
    rounds = []
    for seat in (0, 1):                            # two honest rounds
        deck, w = sh.shuffle_deck(pk, deck)
        rounds.append(da.ShuffleRound(seat=seat, output=deck, witness=w))
    # seat 2 "shuffles" the TRIVIAL deck instead: it knows this permutation
    forged, forged_w = sh.shuffle_deck(pk, eg.make_trivial_deck())
    rounds.append(da.ShuffleRound(seat=2, output=forged, witness=forged_w))

    chain = da.ShuffleChain(pk=pk, rounds=rounds)
    rep = da.audit_deck(forged, Xs, _all_shares(forged, xs), chain=chain)
    assert rep.ok is False
    assert rep.bad_seats == [2]
    assert any("round 3 (seat 2)" in p and "not a shuffle of its input" in p
               for p in rep.problems)
    # and the multiset view alone really would have accepted it:
    assert sorted(c for c in rep.cards if c) == sorted(eg.CARDS)


def test_unopened_witness_is_attributed():
    xs, Xs, pk, deck, chain = _table()
    rounds = list(chain.rounds)
    rounds[1] = da.ShuffleRound(seat=rounds[1].seat, output=rounds[1].output,
                                witness=None)
    rep = da.audit_deck(deck, Xs, _all_shares(deck, xs),
                        chain=da.ShuffleChain(pk=pk, rounds=rounds))
    assert rep.ok is False
    assert rep.bad_seats == [rounds[1].seat]
    assert any("witness not opened" in p for p in rep.problems)


def test_deck_that_is_not_the_chain_output_is_rejected():
    """A valid chain for a DIFFERENT deck proves nothing about this one."""
    xs, Xs, pk, deck, chain = _table()
    other, _ = sh.shuffle_deck(pk, deck)           # an honest-looking extra step
    rep = da.audit_deck(other, Xs, _all_shares(other, xs), chain=chain)
    assert rep.ok is False
    assert any("not the output of the shuffle chain" in p for p in rep.problems)


def test_missing_chain_is_never_ok():
    xs, Xs, pk, deck, chain = _table()
    rep = da.audit_deck(deck, Xs, _all_shares(deck, xs), chain=None)
    assert rep.ok is False
    assert any("not bound to a shuffle chain" in p for p in rep.problems)
    # decryption still ran, so the cards are still reported
    assert sorted(rep.cards) == sorted(eg.CARDS)


def test_chain_is_a_required_argument():
    xs, Xs, pk, deck, chain = _table()
    with pytest.raises(TypeError):
        da.audit_deck(deck, Xs, _all_shares(deck, xs))   # no chain= at all


def test_witness_for_a_different_permutation_is_rejected():
    xs, Xs, pk, deck, chain = _table(n_seats=2)
    r0 = chain.rounds[0]
    perm = list(r0.witness.perm)
    perm[0], perm[1] = perm[1], perm[0]            # lie about two positions
    bad = sh.ShuffleWitness(perm=perm, scalars=list(r0.witness.scalars))
    rounds = [da.ShuffleRound(seat=r0.seat, output=r0.output, witness=bad),
              chain.rounds[1]]
    rep = da.audit_deck(deck, Xs, _all_shares(deck, xs),
                        chain=da.ShuffleChain(pk=pk, rounds=rounds))
    assert rep.ok is False
    assert rep.bad_seats == [r0.seat]


def test_first_corrupt_round_none_when_clean():
    xs, Xs, pk, deck, chain = _table()
    rep = da.audit_deck(deck, Xs, _all_shares(deck, xs), chain=chain)
    assert da.first_corrupt_round([rep, rep]) is None


if __name__ == "__main__":
    # Preserve fixtures, parametrization and a failing process exit status.
    raise SystemExit(pytest.main([__file__, *sys.argv[1:]]))
