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

from crypto_gate import initialize_for_tests, NativeCryptoUnavailable

try:
    initialize_for_tests()
    from holdem.p2p import ristretto as R
    from holdem.p2p import pedersen as P
except NativeCryptoUnavailable as exc:
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


@pytest.mark.parametrize("seed,expected,digest", [
    (b"nums", ["8ccb17b9cd0d00048d21d33ddc3992e73684e3b169f5b121046a082ddd9a8877",
               "18146ba815d2626df1177af0741365dae696049f3d447356e955ecce8404542b",
               "160b3fbb7b3a786a22c1a60e8923a6e19c61cec153d4891ae0f65b545ba31144"],
     "352ddae50082aca6a0709154fb1a42cd8c46c683efca66828b48c2bb7bc38825"),
    (b"other", ["aa5864e4c997cd924da2a17aac0235e2e8195fd006ee6f1cf25710565cac764b",
                "263b688b12a42cbfb408db162e3740ab272a8b4c7bd346bc017bf623a005f154",
                "6a4305b40c690215202a65530b42db269b40c5ebe0711c395ec79da42cb9a265"],
     "92c6a45b5dd20084562ca1cfa47c525d9e8bf2566df7d5aa75c2d393cfd8afb9"),
])
def test_generators_are_nums(seed, expected, digest):
    # Fixed vectors independently generated via ctypes calling libsodium's
    # crypto_core_ristretto255_from_hash(SHA512(domain|seed|label|uint32be(index))).
    # Domain: poker.bg.pedersen.gen.v1; H/0 then G/0 through G/12.
    # digest is SHA256 of all fourteen native point encodings in that order.
    # No production derivation helper is used to construct the oracle.
    ck = P.CommitmentKey.generate(13, seed=seed)
    points = [ck.H, *ck.Gs]
    assert [bytes(p).hex() for p in points[:3]] == expected
    assert hashlib.sha256(b"".join(bytes(p) for p in points)).hexdigest() == digest
    assert all(bytes(p) != bytes(32) for p in points)
    assert len({bytes(p) for p in points}) == 14
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
    raise SystemExit(pytest.main([__file__, *sys.argv[1:]]))
