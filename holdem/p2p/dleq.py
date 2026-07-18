"""DLEQ (discrete-log equality) proofs over Ristretto255 (mental-poker L3).

Implements MULTIPLAYER.md Phase 2 section 3's proof-of-correct-decryption:
a Chaum-Pedersen sigma protocol, made non-interactive by Fiat-Shamir.

Purpose
-------
When seat i publishes a partial decryption D_i = x_i * C0 of a card
ciphertext, everyone else must be able to check that D_i really was
computed with the *same* secret x_i that defines i's public key share
X_i = x_i * G -- without i revealing x_i. Otherwise a seat could submit a
garbage share (corrupting the deal) while appearing to cooperate.

A DLEQ proof demonstrates:  log_G(X_i) == log_C0(D_i)
i.e. the same scalar x_i satisfies X_i = x_i*G AND D_i = x_i*C0.

Construction (Chaum-Pedersen + Fiat-Shamir)
-------------------------------------------
Prover knows x with X = x*G and D = x*C0. It picks a random nonce k and
commits R1 = k*G, R2 = k*C0. The challenge c is the hash of the whole
transcript (domain-separated), and the response is s = k - x*c in the
scalar field. The proof is (c, s), 64 bytes.

Verifier recomputes R1' = s*G + c*X and R2' = s*C0 + c*D (which equal the
prover's R1, R2 iff the relation holds) and checks that hashing the
transcript reproduces c.

Adaptations from the spec pseudocode
------------------------------------
* Scalar arithmetic uses libsodium's constant-time field ops
  (scalar_mul / scalar_sub) rather than hand-rolled ``% Q`` bignum math:
  same result, but constant-time and with no group-order constant to get
  wrong.
* The Fiat-Shamir challenge is SHA-512 reduced into the scalar field via
  ``scalar_reduce`` (64-byte reduction, negligible bias), rather than
  SHA-256 mod Q (which is slightly biased). The transcript bytes hashed
  are identical in spirit: a domain tag followed by G, X, C0, D, R1, R2.
"""
from __future__ import annotations

import hashlib

from holdem.p2p import ristretto as R
from holdem.p2p.ristretto import Point, Scalar


_DOMAIN = b"poker.dleq.v1|"
PROOF_BYTES = 64   # c (32) || s (32)


def _challenge(X: Point, D: Point, C0: Point, R1: Point, R2: Point) -> Scalar:
    """Fiat-Shamir challenge scalar over the full proof transcript."""
    h = hashlib.sha512(
        _DOMAIN + bytes(R.G) + bytes(X) + bytes(D) + bytes(C0)
        + bytes(R1) + bytes(R2)
    ).digest()
    return R.scalar_reduce(h)


def prove(x: Scalar, C0: Point) -> bytes:
    """Prove that D = x*C0 shares its discrete log with X = x*G.

    Returns a 64-byte proof (c || s). ``x`` is the seat's secret share;
    ``C0`` is the ciphertext component being partially decrypted.
    """
    X = R.mul_base(x)          # public key share  X = x*G
    D = R.mul(x, C0)           # partial decrypt   D = x*C0

    k = R.random_scalar()
    R1 = R.mul_base(k)         # k*G
    R2 = R.mul(k, C0)          # k*C0

    c = _challenge(X, D, C0, R1, R2)
    # s = k - x*c   (scalar field)
    s = R.scalar_sub(k, R.scalar_mul(x, c))
    return bytes(c) + bytes(s)


def verify(X: Point, D: Point, C0: Point, proof: bytes) -> bool:
    """Check a DLEQ proof that D = x*C0 for the same x with X = x*G.

    ``X`` is the prover's public key share, ``D`` its claimed partial
    decryption, ``C0`` the ciphertext component. Returns True iff valid.
    """
    if len(proof) != PROOF_BYTES:
        return False
    try:
        c = Scalar(proof[:32])
        s = Scalar(proof[32:])
        # R1' = s*G + c*X ;  R2' = s*C0 + c*D
        R1 = R.add(R.mul_base(s), R.mul(c, X))
        R2 = R.add(R.mul(s, C0), R.mul(c, D))
    except ValueError:
        return False

    c_expected = _challenge(X, D, C0, R1, R2)
    # constant-time-ish equality on the 32-byte challenge
    return bytes(c) == bytes(c_expected)


__all__ = ["prove", "verify", "PROOF_BYTES"]
