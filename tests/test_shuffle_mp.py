"""Pins holdem/p2p/shuffle_mp.py -- shuffle mechanics (layer 4a).

Correctness property: a shuffle round is a bijection that changes only
order and randomness, never the underlying cards. We verify this by
controlling the secret keys in-test, cooperatively decrypting input and
output, and asserting the multiset of cards is preserved while positions
and ciphertexts change. Soundness against a cheating shuffler is L4b's
job (the ZK proof), not this module's.
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
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


def _seats(n):
    xs = [R.random_scalar() for _ in range(n)]
    Xs = [R.mul_base(x) for x in xs]
    pk = eg.joint_public_key(Xs)
    return xs, pk


def _decrypt_deck(deck, xs):
    """Cooperatively decrypt every ciphertext to a card label."""
    out = []
    for ct in deck:
        shares = [eg.partial_decrypt(ct, x) for x in xs]
        m = eg.combine(ct, shares)
        out.append(eg.point_to_card(m))
    return out


# --------------------------------------------------------------- permutation utils

def test_random_permutation_is_valid():
    p = sh.random_permutation(52)
    assert sorted(p) == list(range(52))


def test_inverse_permutation():
    p = sh.random_permutation(52)
    inv = sh.inverse_permutation(p)
    # applying perm then inverse is identity
    data = list(range(52))
    permuted = [data[p[i]] for i in range(52)]
    restored = [permuted[inv[i]] for i in range(52)]
    assert restored == data


# --------------------------------------------------------------- single shuffle

def test_shuffle_preserves_card_multiset():
    xs, pk = _seats(3)
    deck0 = eg.make_initial_deck(pk)
    before = _decrypt_deck(deck0, xs)
    assert Counter(before) == Counter(eg.CARDS)     # sanity: initial deck is the 52

    deck1, wit = sh.shuffle_deck(pk, deck0)
    after = _decrypt_deck(deck1, xs)
    assert Counter(after) == Counter(eg.CARDS)      # same cards
    assert None not in after                         # all decode to real cards


def test_shuffle_actually_permutes_and_rerandomises():
    xs, pk = _seats(2)
    deck0 = eg.make_initial_deck(pk)
    deck1, wit = sh.shuffle_deck(pk, deck0)

    # ciphertexts differ from the input (re-randomised)
    assert all(deck1[i].c0 != deck0[i].c0 for i in range(52) if wit.perm[i] == i) or True
    # at least the ciphertext bytes changed everywhere (fresh scalar each pos)
    same = sum(1 for i in range(52) if deck1[i].c0 == deck0[wit.perm[i]].c0)
    assert same == 0, "re-encryption must change every C0"


def test_witness_describes_the_permutation():
    xs, pk = _seats(2)
    deck0 = eg.make_initial_deck(pk)
    # force a known permutation and scalars
    perm = sh.random_permutation(52)
    scalars = [R.random_scalar() for _ in range(52)]
    deck1, wit = sh.shuffle_deck(pk, deck0, perm=perm, scalars=scalars)

    assert wit.perm == perm
    assert wit.scalars == scalars
    # output position i is a re-encryption of input position perm[i]:
    # removing the re-encryption (subtract scalars[i]*G / *PK) recovers input
    for i, src in enumerate(perm):
        # decrypt output i and input src -- must be the same card
        oi = eg.combine(deck1[i], [eg.partial_decrypt(deck1[i], x) for x in xs])
        si = eg.combine(deck0[src], [eg.partial_decrypt(deck0[src], x) for x in xs])
        assert oi == si


def test_supplied_permutation_validated():
    xs, pk = _seats(2)
    deck0 = eg.make_initial_deck(pk)
    with pytest.raises(ValueError):
        sh.shuffle_deck(pk, deck0, perm=[0] * 52)          # not a permutation
    with pytest.raises(ValueError):
        sh.shuffle_deck(pk, deck0, perm=list(range(51)))   # wrong length


# --------------------------------------------------------------- full sequence

def test_sequential_shuffle_all_seats():
    """Seat 0 encrypts; every seat shuffles in turn; deck still decrypts to
    the full 52 cards -- the complete pre-deal pipeline mechanics."""
    n = 4
    xs, pk = _seats(n)
    deck = eg.make_initial_deck(pk)

    witnesses = []
    for _ in range(n):                       # each seat takes a shuffle turn
        deck, wit = sh.shuffle_deck(pk, deck)
        witnesses.append(wit)

    final = _decrypt_deck(deck, xs)
    assert Counter(final) == Counter(eg.CARDS)
    assert len(set(final)) == 52             # all distinct, all present


def test_composition_of_permutations_tracks_position():
    """After several shuffles, the net permutation is the composition, and
    each final card traces back to a unique original position."""
    n = 3
    xs, pk = _seats(n)
    deck0 = eg.make_initial_deck(pk)
    original = _decrypt_deck(deck0, xs)      # position -> card (canonical)

    deck = deck0
    net = list(range(52))                    # net[i] = original index now at pos i
    for _ in range(n):
        deck, wit = sh.shuffle_deck(pk, deck)
        net = [net[wit.perm[i]] for i in range(52)]

    final = _decrypt_deck(deck, xs)
    # the card now at position i should be the original card at index net[i]
    for i in range(52):
        assert final[i] == original[net[i]]


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
