"""Pins holdem/p2p/dleq.py -- Chaum-Pedersen DLEQ proofs (layer 3).

Soundness is the point: a DLEQ proof is only useful if it REJECTS every
dishonest share. These tests check the happy path once and then hammer
the rejection cases -- wrong secret, tampered share/ciphertext/pubkey,
mutated proof bytes -- plus integration with the real ElGamal partial
decryption from layer 2.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import ristretto as R
    from holdem.p2p import dleq
    from holdem.p2p import elgamal as eg
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


def _setup():
    """A secret x, its public share X=x*G, and a random C0."""
    x = R.random_scalar()
    X = R.mul_base(x)
    C0 = R.mul_base(R.random_scalar())     # any point stands in for a ciphertext C0
    D = R.mul(x, C0)                         # honest partial decrypt
    return x, X, C0, D


# --------------------------------------------------------------- happy path

def test_valid_proof_verifies():
    x, X, C0, D = _setup()
    proof = dleq.prove(x, C0)
    assert dleq.verify(X, D, C0, proof) is True


def test_proof_is_64_bytes():
    x, X, C0, D = _setup()
    assert len(dleq.prove(x, C0)) == dleq.PROOF_BYTES == 64


def test_proofs_are_randomised():
    # different nonces -> different proofs, both valid
    x, X, C0, D = _setup()
    p1 = dleq.prove(x, C0)
    p2 = dleq.prove(x, C0)
    assert p1 != p2
    assert dleq.verify(X, D, C0, p1)
    assert dleq.verify(X, D, C0, p2)


# --------------------------------------------------------------- soundness

def test_wrong_secret_share_is_rejected():
    """D computed with a different secret than X must fail."""
    x, X, C0, _ = _setup()
    y = R.random_scalar()                   # attacker's different secret
    D_bad = R.mul(y, C0)                     # share under the wrong key
    proof = dleq.prove(y, C0)               # honestly proves for y...
    # ...but verified against X (which is x*G) -> mismatch
    assert dleq.verify(X, D_bad, C0, proof) is False


def test_tampered_share_is_rejected():
    x, X, C0, D = _setup()
    proof = dleq.prove(x, C0)
    D_tampered = R.add(D, R.G)              # nudge the share off by G
    assert dleq.verify(X, D_tampered, C0, proof) is False


def test_wrong_ciphertext_is_rejected():
    x, X, C0, D = _setup()
    proof = dleq.prove(x, C0)
    C0_other = R.mul_base(R.random_scalar())
    assert dleq.verify(X, D, C0_other, proof) is False


def test_wrong_pubkey_is_rejected():
    x, X, C0, D = _setup()
    proof = dleq.prove(x, C0)
    X_other = R.mul_base(R.random_scalar())
    assert dleq.verify(X_other, D, C0, proof) is False


def test_mutated_proof_bytes_rejected():
    x, X, C0, D = _setup()
    proof = bytearray(dleq.prove(x, C0))
    proof[0] ^= 0x01                        # flip a bit in c
    assert dleq.verify(X, D, C0, bytes(proof)) is False
    proof2 = bytearray(dleq.prove(x, C0))
    proof2[40] ^= 0x01                      # flip a bit in s
    assert dleq.verify(X, D, C0, bytes(proof2)) is False


def test_wrong_length_proof_rejected():
    x, X, C0, D = _setup()
    assert dleq.verify(X, D, C0, b"\x00" * 63) is False
    assert dleq.verify(X, D, C0, b"\x00" * 65) is False
    assert dleq.verify(X, D, C0, b"") is False


def test_zero_proof_rejected():
    x, X, C0, D = _setup()
    assert dleq.verify(X, D, C0, b"\x00" * 64) is False


# --------------------------------------------------------------- integration

def test_dleq_proves_real_elgamal_partial_decrypt():
    """A seat's real ElGamal partial decrypt, proven honest via DLEQ."""
    x = R.random_scalar()
    X = R.mul_base(x)
    pk = X                                   # single-seat joint key = X
    m = eg.card_point("As")
    ct = eg.encrypt(pk, m)

    D = eg.partial_decrypt(ct, x)           # = x * ct.c0
    proof = dleq.prove(x, ct.c0)
    assert dleq.verify(X, D, ct.c0, proof) is True

    # and the decrypt actually recovers the card
    assert eg.combine(ct, [D]) == m


def test_dleq_catches_lying_seat_in_multiparty_deal():
    """In a 3-seat deal, a seat submitting a bogus share is caught by DLEQ
    even though the share 'looks like' a point."""
    xs = [R.random_scalar() for _ in range(3)]
    Xs = [R.mul_base(x) for x in xs]
    pk = eg.joint_public_key(Xs)
    ct = eg.encrypt(pk, eg.card_point("Kd"))

    # seat 1 lies: submits a share under a different secret
    liar = R.random_scalar()
    D_bogus = R.mul(liar, ct.c0)
    proof_bogus = dleq.prove(liar, ct.c0)          # valid proof for the WRONG key
    assert dleq.verify(Xs[1], D_bogus, ct.c0, proof_bogus) is False

    # honest seats verify fine
    for i in (0, 2):
        D = eg.partial_decrypt(ct, xs[i])
        p = dleq.prove(xs[i], ct.c0)
        assert dleq.verify(Xs[i], D, ct.c0, p) is True


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
