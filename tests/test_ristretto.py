"""Pins holdem/p2p/ristretto.py -- the libsodium Ristretto255 wrapper.

These tests assert the algebraic laws the mental-poker protocol relies
on, not merely that the functions execute. If the group axioms don't
hold, every layer above (ElGamal, DLEQ, the shuffle) is unsound.
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import ristretto as R
except RuntimeError as exc:  # libsodium not available on this machine
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


def _wide(seed: bytes) -> bytes:
    return hashlib.sha512(seed).digest()


# --------------------------------------------------------------- sanity

def test_library_loads_and_reports_version():
    v = R.libsodium_version()
    assert v and isinstance(v, str)
    assert R.POINT_BYTES == 32 and R.SCALAR_BYTES == 32 and R.HASH_BYTES == 64


def test_types_reject_wrong_length():
    with pytest.raises(ValueError):
        R.Point(b"\x00" * 31)
    with pytest.raises(ValueError):
        R.Scalar(b"\x00" * 33)


def test_point_and_scalar_are_distinct_types():
    s = R.random_scalar()
    p = R.mul_base(s)
    assert isinstance(s, R.Scalar) and not isinstance(s, R.Point)
    assert isinstance(p, R.Point) and not isinstance(p, R.Scalar)


# --------------------------------------------------------------- group axioms

def test_addition_is_commutative():
    a = R.mul_base(R.random_scalar())
    b = R.mul_base(R.random_scalar())
    assert R.add(a, b) == R.add(b, a)


def test_addition_is_associative():
    a = R.mul_base(R.random_scalar())
    b = R.mul_base(R.random_scalar())
    c = R.mul_base(R.random_scalar())
    assert R.add(R.add(a, b), c) == R.add(a, R.add(b, c))


def test_sub_is_inverse_of_add():
    a = R.mul_base(R.random_scalar())
    b = R.mul_base(R.random_scalar())
    assert R.sub(R.add(a, b), b) == a


def test_scalarmult_distributes_over_point_add():
    # k*(A+B) == k*A + k*B
    k = R.random_scalar()
    A = R.mul_base(R.random_scalar())
    B = R.mul_base(R.random_scalar())
    lhs = R.mul(k, R.add(A, B))
    rhs = R.add(R.mul(k, A), R.mul(k, B))
    assert lhs == rhs


def test_scalar_add_matches_point_add_under_base():
    # (r1+r2)*G == r1*G + r2*G  -- the shuffle re-encryption identity
    r1 = R.random_scalar()
    r2 = R.random_scalar()
    lhs = R.mul_base(R.scalar_add(r1, r2))
    rhs = R.add(R.mul_base(r1), R.mul_base(r2))
    assert lhs == rhs


def test_scalar_mul_matches_repeated_scalarmult():
    # (a*b)*G == a*(b*G)
    a = R.random_scalar()
    b = R.random_scalar()
    lhs = R.mul_base(R.scalar_mul(a, b))
    rhs = R.mul(a, R.mul_base(b))
    assert lhs == rhs


def test_scalar_negate_gives_point_negation():
    # k*G + (-k)*G is the identity, so P + k*G + (-k)*G == P.
    k = R.random_scalar()
    P = R.mul_base(R.random_scalar())
    kG = R.mul_base(k)
    negkG = R.mul_base(R.scalar_negate(k))
    assert R.add(R.add(P, kG), negkG) == P


def test_scalar_invert_round_trips():
    a = R.random_scalar()
    inv = R.scalar_invert(a)
    one = R.scalar_from_bytes((1).to_bytes(32, "little"))
    G = R.mul_base(one)
    prod = R.scalar_mul(a, inv)
    assert R.mul_base(prod) == G


# --------------------------------------------------------------- hash-to-group

def test_hash_to_group_deterministic_and_valid():
    w = _wide(b"poker.card.v1:0:2c")
    p1 = R.hash_to_group(w)
    p2 = R.hash_to_group(w)
    assert p1 == p2
    assert p1.is_valid()


def test_hash_to_group_distinct_inputs_distinct_points():
    pts = {R.hash_to_group(_wide(f"card:{i}".encode())) for i in range(52)}
    assert len(pts) == 52  # no collisions across a full deck


def test_hash_to_group_rejects_wrong_length():
    with pytest.raises(ValueError):
        R.hash_to_group(b"\x00" * 32)


# --------------------------------------------------------------- wire parsing

def test_point_from_bytes_accepts_valid():
    p = R.mul_base(R.random_scalar())
    parsed = R.point_from_bytes(bytes(p))
    assert parsed == p


def test_point_from_bytes_rejects_garbage():
    # all-0xFF is not a canonical ristretto encoding
    with pytest.raises(ValueError):
        R.point_from_bytes(b"\xff" * 32)


# --------------------------------------------------------------- ElGamal

def test_elgamal_encrypt_decrypt_round_trip():
    """The core deal operation: encrypt a card point, cooperatively decrypt."""
    x = R.random_scalar()          # secret key
    PK = R.mul_base(x)             # public key
    M = R.hash_to_group(_wide(b"card:As"))

    r = R.random_scalar()
    C0 = R.mul_base(r)             # r*G
    C1 = R.add(M, R.mul(r, PK))    # M + r*PK

    # decrypt: M = C1 - x*C0
    M_rec = R.sub(C1, R.mul(x, C0))
    assert M_rec == M


def test_elgamal_reencrypt_preserves_plaintext():
    """Re-encryption (the shuffle's core move) must not change the plaintext."""
    x = R.random_scalar()
    PK = R.mul_base(x)
    M = R.hash_to_group(_wide(b"card:Kd"))

    r = R.random_scalar()
    C0 = R.mul_base(r)
    C1 = R.add(M, R.mul(r, PK))

    # re-encrypt with a fresh scalar r'
    r2 = R.random_scalar()
    C0b = R.add(C0, R.mul_base(r2))
    C1b = R.add(C1, R.mul(r2, PK))

    # still decrypts to M
    M_rec = R.sub(C1b, R.mul(x, C0b))
    assert M_rec == M


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
