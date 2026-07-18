"""Pins holdem/p2p/elgamal.py -- deck encoding and threshold ElGamal.

The key tests exercise the *multi-party* scheme the protocol actually
uses: a joint public key that is the sum of per-seat key shares, with
decryption combining one partial-decrypt share per seat. If that flow is
wrong, the deal is wrong.
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import ristretto as R
    from holdem.p2p import elgamal as EG
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


# --------------------------------------------------------------- deck encoding

def test_canonical_card_order():
    assert len(EG.CARDS) == 52
    assert EG.CARDS[0] == "2c"
    assert EG.CARDS[12] == "Ac"
    assert EG.CARDS[13] == "2d"
    assert EG.CARDS[51] == "As"
    assert len(set(EG.CARDS)) == 52


def test_card_points_are_valid_and_distinct():
    pts = EG.deck_points()
    assert len(pts) == 52
    assert all(p.is_valid() for p in pts)
    assert len({bytes(p) for p in pts}) == 52   # no collisions


def test_card_point_is_deterministic():
    # Stable across calls and a fresh module concept: same label -> same point.
    a = EG.card_point("As")
    b = EG.card_point("As")
    assert a == b
    # matches the documented construction exactly
    expect = R.hash_to_group(hashlib.sha512(b"poker.card.v1:51:As").digest())
    assert a == expect


def test_point_to_card_round_trip():
    for c in EG.CARDS:
        p = EG.card_point(c)
        assert EG.point_to_card(p) == c


def test_point_to_card_rejects_non_card():
    stranger = R.mul_base(R.random_scalar())
    # astronomically unlikely to be a card point
    assert EG.point_to_card(stranger) is None


# --------------------------------------------------------------- single-key ElGamal

def test_encrypt_decrypt_single_key():
    x = R.random_scalar()
    pk = R.mul_base(x)
    m = EG.card_point("Kd")
    ct = EG.encrypt(pk, m)
    share = EG.partial_decrypt(ct, x)
    assert EG.combine(ct, [share]) == m


def test_reencrypt_preserves_plaintext():
    x = R.random_scalar()
    pk = R.mul_base(x)
    m = EG.card_point("7h")
    ct = EG.encrypt(pk, m)
    ct2 = EG.reencrypt(pk, ct)
    ct3 = EG.reencrypt(pk, ct2)
    # ciphertext changed each time...
    assert ct.to_hex() != ct2.to_hex() != ct3.to_hex()
    # ...but plaintext is stable
    share = EG.partial_decrypt(ct3, x)
    assert EG.combine(ct3, [share]) == m


def test_ciphertext_hex_round_trip():
    x = R.random_scalar()
    pk = R.mul_base(x)
    ct = EG.encrypt(pk, EG.card_point("2c"))
    again = EG.Ciphertext.from_hex(ct.to_hex())
    assert again == ct


def test_from_hex_rejects_bad_point():
    with pytest.raises(ValueError):
        EG.Ciphertext.from_hex(["ff" * 32, "ff" * 32])


# --------------------------------------------------------------- MULTI-PARTY (the real scheme)

def _joint_key(shares):
    """PK = sum of per-seat public shares X_i = x_i * G."""
    pubs = [R.mul_base(x) for x in shares]
    pk = pubs[0]
    for p in pubs[1:]:
        pk = R.add(pk, p)
    return pk


def test_threshold_encrypt_decrypt_three_seats():
    """The actual deal: 3 seats, joint key, combine 3 partial decryptions."""
    xs = [R.random_scalar() for _ in range(3)]
    pk = _joint_key(xs)
    m = EG.card_point("Ah")

    ct = EG.encrypt(pk, m)
    shares = [EG.partial_decrypt(ct, x) for x in xs]
    assert EG.combine(ct, shares) == m


def test_threshold_survives_shuffle_reencryption():
    """Full flow: joint-key encrypt, several seats re-encrypt, then all
    seats partial-decrypt and combine -> original card."""
    xs = [R.random_scalar() for _ in range(4)]
    pk = _joint_key(xs)
    m = EG.card_point("Qs")

    ct = EG.encrypt(pk, m)
    # each seat re-encrypts in turn (the shuffle's per-seat move)
    for _ in range(4):
        ct = EG.reencrypt(pk, ct)

    shares = [EG.partial_decrypt(ct, x) for x in xs]
    assert EG.combine(ct, shares) == m


def test_partial_decrypt_order_does_not_matter():
    xs = [R.random_scalar() for _ in range(3)]
    pk = _joint_key(xs)
    m = EG.card_point("Td")
    ct = EG.encrypt(pk, m)
    shares = [EG.partial_decrypt(ct, x) for x in xs]
    import itertools
    for perm in itertools.permutations(shares):
        assert EG.combine(ct, list(perm)) == m


def test_missing_share_does_not_decrypt():
    """Omitting a seat's share yields the wrong point -- no card recovered."""
    xs = [R.random_scalar() for _ in range(3)]
    pk = _joint_key(xs)
    m = EG.card_point("9c")
    ct = EG.encrypt(pk, m)
    shares = [EG.partial_decrypt(ct, x) for x in xs[:-1]]  # drop one
    wrong = EG.combine(ct, shares)
    assert wrong != m
    assert EG.point_to_card(wrong) is None


def test_initial_deck_encrypts_all_52():
    xs = [R.random_scalar() for _ in range(2)]
    pk = _joint_key(xs)
    deck = EG.make_initial_deck(pk)
    assert len(deck) == 52
    # decrypt the whole deck cooperatively -> canonical card set, in order
    recovered = []
    for ct in deck:
        shares = [EG.partial_decrypt(ct, x) for x in xs]
        recovered.append(EG.point_to_card(EG.combine(ct, shares)))
    assert recovered == EG.CARDS


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
