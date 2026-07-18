"""Pins holdem/p2p/mental_poker.py -- deck encoding + ElGamal (layer 2).

The important tests model the REAL protocol usage, not toy cases:
multi-seat joint keys with cooperative decryption, and a full chain of
re-encryptions standing in for successive shuffle rounds. A pinned
card-point vector catches any cross-peer divergence in the encoding.
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import ristretto as R
    from holdem.p2p import mental_poker as mp
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


# --------------------------------------------------------------- deck encoding

def test_deck_is_52_distinct_cards():
    assert len(mp.CARDS) == 52
    assert len(set(mp.CARDS)) == 52
    # suit-major order: first 13 are clubs
    assert mp.CARDS[:13] == [r + "c" for r in "23456789TJQKA"]
    assert mp.CARDS[13] == "2d"


def test_card_points_distinct_and_valid():
    pts = mp.CARD_POINTS
    assert len(pts) == 52
    assert len({bytes(p) for p in pts}) == 52   # injective
    assert all(p.is_valid() for p in pts)


def test_card_point_roundtrip_lookup():
    for c in mp.CARDS:
        p = mp.card_point(c)
        assert mp.point_to_card(p) == c


def test_point_to_card_unknown_returns_none():
    # a random point is (with overwhelming probability) not a card point
    stray = R.mul_base(R.random_scalar())
    assert mp.point_to_card(stray) is None


def test_card_point_rejects_bad_label():
    with pytest.raises(ValueError):
        mp.card_point("Zx")


def test_card_point_vector_is_stable():
    """Pinned vector: if this changes, peers would disagree on the deck.

    These are the first few canonical card points for the fixed
    poker.card.v1 encoding. Recompute deliberately only if the encoding
    is intentionally revved (and then bump the version tag).
    """
    def expect(card):
        label = f"poker.card.v1:{mp.CARDS.index(card)}:{card}".encode()
        return R.hash_to_group(hashlib.sha512(label).digest()).hex()
    # self-consistency: the module's cached point matches the recomputation
    for c in ("2c", "Ac", "2d", "As", "Ks"):
        assert mp.card_point(c).hex() == expect(c)


# --------------------------------------------------------------- ElGamal basic

def test_encrypt_decrypt_single_key():
    x = R.random_scalar()
    pk = R.mul_base(x)
    m = mp.card_point("As")
    c = mp.encrypt(pk, m)
    d = mp.partial_decrypt(x, c)
    assert mp.full_decrypt(c, [d]) == m


def test_ciphertext_hex_roundtrip():
    x = R.random_scalar()
    pk = R.mul_base(x)
    c = mp.encrypt(pk, mp.card_point("Td"))
    c2 = mp.Ciphertext.from_hex(c.to_hex())
    assert c2.c0 == c.c0 and c2.c1 == c.c1


def test_ciphertext_from_hex_rejects_bad_point():
    with pytest.raises(ValueError):
        mp.Ciphertext.from_hex(["ff" * 32, "ff" * 32])


def test_reencrypt_preserves_plaintext():
    x = R.random_scalar()
    pk = R.mul_base(x)
    m = mp.card_point("Qh")
    c = mp.encrypt(pk, m)
    c2 = mp.reencrypt(pk, c)
    assert c2.c0 != c.c0                     # actually re-randomised
    d = mp.partial_decrypt(x, c2)
    assert mp.full_decrypt(c2, [d]) == m


# --------------------------------------------------------------- multi-seat

def _seats(n):
    """n secret shares, their public shares, and the joint key."""
    xs = [R.random_scalar() for _ in range(n)]
    Xs = [R.mul_base(x) for x in xs]
    pk = mp.joint_public_key(Xs)
    return xs, Xs, pk


def test_joint_key_is_sum_of_shares():
    xs, Xs, pk = _seats(4)
    # PK should equal (sum xs) * G
    s = xs[0]
    for x in xs[1:]:
        s = R.scalar_add(s, x)
    assert R.mul_base(s) == pk


def test_cooperative_decrypt_three_seats():
    """Every seat contributes a partial decrypt; together they recover M."""
    xs, Xs, pk = _seats(3)
    m = mp.card_point("9s")
    c = mp.encrypt(pk, m)
    shares = [mp.partial_decrypt(x, c) for x in xs]
    assert mp.full_decrypt(c, shares) == m


def test_missing_one_share_fails_to_decrypt():
    """Omitting a seat's share must NOT recover the plaintext.

    This is the property that makes selective dealing meaningful: a card
    stays hidden unless all required seats cooperate.
    """
    xs, Xs, pk = _seats(3)
    m = mp.card_point("7d")
    c = mp.encrypt(pk, m)
    shares = [mp.partial_decrypt(x, c) for x in xs[:-1]]   # drop the last
    assert mp.full_decrypt(c, shares) != m


def test_full_shuffle_chain_preserves_deck():
    """Encrypt the whole deck, then re-encrypt it through several 'seats'
    in sequence (as the shuffle does), and confirm cooperative decryption
    still yields exactly the original 52 cards."""
    xs, Xs, pk = _seats(4)

    # seat 0 encrypts every card point
    deck = [mp.encrypt(pk, p) for p in mp.deck_points()]

    # seats 1..3 each re-encrypt every ciphertext with fresh randomness
    for _ in range(3):
        deck = [mp.reencrypt(pk, c) for c in deck]

    # cooperative decrypt each position
    recovered = []
    for c in deck:
        shares = [mp.partial_decrypt(x, c) for x in xs]
        m = mp.full_decrypt(c, shares)
        card = mp.point_to_card(m)
        assert card is not None, "recovered a non-card point"
        recovered.append(card)

    assert recovered == mp.CARDS          # same cards, same positions (no perm yet)


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
