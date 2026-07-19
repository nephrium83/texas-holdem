"""Schnorr proof-of-possession for key shares (adopted-plan step 4).

Closes the rogue-key attack at the distributed key-generation ceremony.
The joint encryption key is PK = sum_i X_i where each seat contributes a
public share X_i = x_i*G. If a seat announces its share AFTER seeing the
others', it can set

    X_n = X_target - sum_{i<n} X_i          (X_target = x*.G, x* known)

making PK = X_target, a key it alone controls -- it can then decrypt the
entire deck by itself. The attack works because announcing X_n does not
prove the announcer KNOWS a secret x_n with X_n = x_n*G; the rogue share
is a difference of other points, whose discrete log the attacker does
not know.

The fix: every seat attaches a Schnorr proof of knowledge of the
discrete log of its share, bound to session context. A rogue X_n
constructed as a difference of others' shares has no known discrete log,
so its owner cannot produce this proof. Each seat verifies every other
seat's PoP before summing shares into PK.

Protocol (Schnorr, Fiat-Shamir)
-------------------------------
Prove knowledge of x with X = x*G:
    pick nonce k; R = k*G; c = H(domain | ctx | X | R); s = k + c*x
    proof = (R, s)                                        [32 + 32 bytes]
Verify:  s*G == R + c*X.

``ctx`` MUST bind the ceremony context (session id, and the seat index /
announcement position) so a PoP cannot be replayed from another session
or lifted from another seat. Binding the seat's own identity also stops a
seat from copying a neighbour's (X, proof) pair verbatim -- though that
would announce a colliding share and be rejected anyway, context binding
makes the intent explicit.
"""
from __future__ import annotations

import hashlib

from holdem.p2p import ristretto as R
from holdem.p2p.ristretto import Point, Scalar


_DOMAIN = b"poker.kdkg.pop.v1"
PROOF_BYTES = 64          # R (32) || s (32)


def _challenge(X: Point, commitment: Point, ctx: bytes) -> Scalar:
    h = hashlib.sha512()
    h.update(_DOMAIN)
    h.update(len(ctx).to_bytes(4, "big"))
    h.update(ctx)
    h.update(bytes(X))
    h.update(bytes(commitment))
    return R.scalar_reduce(h.digest())


def prove(x: Scalar, ctx: bytes = b"") -> bytes:
    """Prove knowledge of x for the public share X = x*G.

    Returns a 64-byte proof (R || s). ``ctx`` binds the proof to the key
    ceremony's context and MUST match at verify time.
    """
    X = R.mul_base(x)
    k = R.random_scalar()
    commitment = R.mul_base(k)                  # R = k*G
    c = _challenge(X, commitment, ctx)
    s = R.scalar_add(k, R.scalar_mul(c, x))     # s = k + c*x
    return bytes(commitment) + bytes(s)


def verify(X: Point, proof: bytes, ctx: bytes = b"") -> bool:
    """Verify a proof-of-possession for public share ``X``.

    Returns True iff ``proof`` demonstrates knowledge of x with X = x*G
    under the given context.
    """
    if len(proof) != PROOF_BYTES:
        return False
    try:
        commitment = R.point_from_bytes(proof[:32])   # validates encoding
        s = Scalar(proof[32:])
    except ValueError:
        return False

    c = _challenge(X, commitment, ctx)
    # s*G == R + c*X
    lhs = R.mul_base_safe(s)
    rhs = R.add(commitment, R.mul_safe(c, X))
    return bytes(lhs) == bytes(rhs)


def verify_all(shares, proofs, ctx_for) -> list:
    """Verify a whole ceremony's PoPs. Returns the list of bad seat indices.

    ``shares[i]`` is seat i's public share X_i, ``proofs[i]`` its PoP, and
    ``ctx_for(i)`` yields the context bytes bound for seat i (e.g. session
    id plus the seat index). Empty result means every share is legitimate
    and it is safe to form PK = sum_i X_i.
    """
    bad = []
    for i, (X, pf) in enumerate(zip(shares, proofs)):
        if not verify(X, pf, ctx_for(i)):
            bad.append(i)
    return bad


__all__ = ["PROOF_BYTES", "prove", "verify", "verify_all"]
