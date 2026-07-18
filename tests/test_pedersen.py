"""Pins holdem/p2p/pedersen.py -- generalized Pedersen vector commitment.

The properties that matter for Bayer-Groth: the homomorphic law (the whole
argument manipulates commitments algebraically), NUMS generators (the fix
for the Scytl trapdoor break), correct zero-padding, and binding sanity.
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import ristretto as R
    from holdem.p2p import pedersen as P
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


def _s(i: int) -> R.Scalar:
    """A deterministic non-zero scalar from a small int."""
    return R.scalar_reduce(hashlib.sha512(f"s:{i}".encode()).digest())


# --------------------------------------------------------------- key setup

def test_key_has_requested_generators():
    ck = P.CommitmentKey.generate(13, seed=b"deck")
    assert ck.n == 13
    assert len(ck.Gs) == 13
    # H and all Gs are valid, distinct points
    pts = [ck.H, *ck.Gs]
    assert all(p.is_valid() for p in pts)
    assert len({bytes(p) for p in pts}) == 14


def test_key_is_deterministic_from_seed():
    a = P.CommitmentKey.generate(8, seed=b"same")
    b = P.CommitmentKey.generate(8, seed=b"same")
    assert a.H == b.H and a.Gs == b.Gs


def test_different_seeds_give_different_generators():
    a = P.CommitmentKey.generate(8, seed=b"one")
    b = P.CommitmentKey.generate(8, seed=b"two")
    assert a.H != b.H
    assert a.Gs != b.Gs


def test_generators_are_nums():
    """The anti-Scytl property: every generator recomputes from the seed."""
    ck = P.CommitmentKey.generate(10, seed=b"nums")
    assert ck.verify_nums() is True


def test_verify_nums_catches_substituted_generator():
    ck = P.CommitmentKey.generate(6, seed=b"x")
    # substitute a trapdoored generator (G_3 = t*G for known t) for a hashed one
    t = _s(999)
    tampered = P.CommitmentKey(H=ck.H, Gs=[*ck.Gs[:3], R.mul_base(t), *ck.Gs[4:]],
                               seed=ck.seed)
    assert tampered.verify_nums() is False


# --------------------------------------------------------------- commit basics

def test_commit_single_value_matches_formula():
    ck = P.CommitmentKey.generate(4, seed=b"f")
    a0 = _s(1)
    r = _s(2)
    c = P.commit(ck, [a0], r)
    # r*H + a0*G_0
    expected = R.add(R.mul(r, ck.H), R.mul(a0, ck.Gs[0]))
    assert c == expected


def test_commit_zero_vector_is_rH():
    ck = P.CommitmentKey.generate(4, seed=b"z")
    r = _s(7)
    assert P.commit(ck, [], r) == R.mul(r, ck.H)
    assert P.commit_zero(ck, r) == R.mul(r, ck.H)


def test_commit_accepts_zero_valued_entries():
    """Committing a vector containing zeros must not raise (Scytl-era code
    padded with zeros; libsodium's raw scalarmult would reject them)."""
    ck = P.CommitmentKey.generate(5, seed=b"pad")
    ZERO = R.Scalar(b"\x00" * 32)
    vals = [_s(1), ZERO, _s(3), ZERO, ZERO]
    c = P.commit(ck, vals, _s(4))
    # equals r*H + s1*G0 + s3*G2 (the zero terms drop out)
    expected = R.add(R.add(R.mul(_s(4), ck.H), R.mul(_s(1), ck.Gs[0])),
                     R.mul(_s(3), ck.Gs[2]))
    assert c == expected


def test_commit_rejects_too_many_values():
    ck = P.CommitmentKey.generate(3, seed=b"o")
    with pytest.raises(ValueError):
        P.commit(ck, [_s(1), _s(2), _s(3), _s(4)], _s(5))


# --------------------------------------------------------------- homomorphism

def test_homomorphic_addition():
    """comck(a;r) + comck(b;s) == comck(a+b; r+s) -- the core BG property."""
    ck = P.CommitmentKey.generate(6, seed=b"hom")
    a = [_s(i) for i in range(1, 7)]
    b = [_s(i) for i in range(11, 17)]
    r, s = _s(100), _s(200)

    lhs = R.add(P.commit(ck, a, r), P.commit(ck, b, s))
    ab = [R.scalar_add(ai, bi) for ai, bi in zip(a, b)]
    rs = R.scalar_add(r, s)
    rhs = P.commit(ck, ab, rs)
    assert lhs == rhs


def test_homomorphic_scalar_multiple():
    """k * comck(a; r) == comck(k*a; k*r)."""
    ck = P.CommitmentKey.generate(4, seed=b"scal")
    a = [_s(i) for i in range(1, 5)]
    r = _s(50)
    k = _s(9)

    lhs = R.mul(k, P.commit(ck, a, r))
    ka = [R.scalar_mul(k, ai) for ai in a]
    kr = R.scalar_mul(k, r)
    rhs = P.commit(ck, ka, kr)
    assert lhs == rhs


# --------------------------------------------------------------- binding sanity

def test_different_vectors_give_different_commitments():
    ck = P.CommitmentKey.generate(4, seed=b"bind")
    r = _s(3)
    c1 = P.commit(ck, [_s(1), _s(2), _s(3), _s(4)], r)
    c2 = P.commit(ck, [_s(1), _s(2), _s(3), _s(5)], r)   # last entry differs
    assert c1 != c2


def test_different_randomness_gives_different_commitment():
    ck = P.CommitmentKey.generate(4, seed=b"rand")
    vals = [_s(1), _s(2), _s(3), _s(4)]
    assert P.commit(ck, vals, _s(1)) != P.commit(ck, vals, _s(2))


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
