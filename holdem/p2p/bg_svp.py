"""Bayer-Groth single-value product argument (paper section 5.3).

Proves knowledge of an opening a_1..a_n, r of a Pedersen vector commitment
c_a = comck(a; r) such that the committed values have a given public
product b = prod a_i. This is the leaf of the product-argument subtree of
the shuffle argument: the Hadamard argument (5.1) reduces "committed
matrix has row-products b_i" to it.

Protocol (paper, restated additively)
-------------------------------------
Witness: a_1..a_n, r with c_a = comck(a; r), b = prod a_i. Let b_i be the
partial products b_1 = a_1, b_i = b_{i-1} * a_i (so b_n = b).

Initial message: pick d_1..d_n, r_d at random; set delta_1 = d_1,
delta_n = 0, pick delta_2..delta_{n-1} at random; pick s_1, s_x. Send

    c_d     = comck(d_1..d_n; r_d)
    c_delta = comck(-delta_1*d_2, ..., -delta_{n-1}*d_n; s_1)
    c_Delta = comck(delta_2 - a_2*delta_1 - b_1*d_2, ...,
                    delta_n - a_n*delta_{n-1} - b_{n-1}*d_n; s_x)

Challenge x (Fiat-Shamir here), then the answer

    a~_i = x*a_i + d_i        r~ = x*r + r_d
    b~_i = x*b_i + delta_i    s~ = x*s_x + s_1

Verification:
    x*c_a + c_d      == comck(a~_1..a~_n; r~)
    x*c_Delta + c_delta
                     == comck(x*b~_2 - b~_1*a~_2, ...,
                              x*b~_n - b~_{n-1}*a~_n; s~)
    b~_1 == a~_1     and     b~_n == x*b

Soundness intuition: each verified entry x*b~_{i+1} - b~_i*a~_{i+1} is a
polynomial in x whose x^2 coefficient is b_{i+1} - b_i*a_{i+1}; the two
commitments fix only the x^1 and x^0 coefficients, so by Schwartz-Zippel
a prover has negligible chance over x unless every b_{i+1} = b_i*a_{i+1},
which chains b~_1 = a~_1 up to b~_n = x*b into b = prod a_i.

Fiat-Shamir (Scytl pitfall #2): the challenge hashes the COMPLETE
transcript -- domain tag, the full commitment key (every generator, not
just a seed reference), the statement (c_a, n, b), and every prover
message (c_d, c_delta, c_Delta). Nothing the verifier checks is outside
the hash.

n >= 2 is required: with n == 1 the construction forces d_1 = delta_1 =
delta_n = 0, which destroys the zero-knowledge blinding of a~_1.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import List, Sequence

from holdem.p2p import ristretto as R
from holdem.p2p.ristretto import Point, Scalar
from holdem.p2p.pedersen import CommitmentKey, commit


_DOMAIN = b"poker.bg.svp.v1"


@dataclass(frozen=True)
class SVPProof:
    """Non-interactive single-value product proof."""
    c_d: Point
    c_delta: Point
    c_Delta: Point
    a_tilde: List[Scalar]
    b_tilde: List[Scalar]
    r_tilde: Scalar
    s_tilde: Scalar


def _challenge(ck: CommitmentKey, c_a: Point, n: int, b: Scalar,
               c_d: Point, c_delta: Point, c_Delta: Point) -> Scalar:
    """Fiat-Shamir challenge over the complete transcript."""
    h = hashlib.sha512()
    h.update(_DOMAIN)
    h.update(bytes(ck.H))
    for Gi in ck.Gs:
        h.update(bytes(Gi))
    h.update(bytes(c_a))
    h.update(n.to_bytes(4, "big"))
    h.update(bytes(b))
    h.update(bytes(c_d))
    h.update(bytes(c_delta))
    h.update(bytes(c_Delta))
    x = R.scalar_reduce(h.digest())
    if R.is_zero_scalar(x):                     # probability ~2^-252
        raise ValueError("Fiat-Shamir challenge reduced to zero")
    return x


def prove(ck: CommitmentKey, a: Sequence[Scalar], r: Scalar,
          b: Scalar) -> SVPProof:
    """Prove that comck(a; r) opens to values whose product is b."""
    n = len(a)
    if n < 2:
        raise ValueError("single-value product argument requires n >= 2")
    if n > ck.n:
        raise ValueError(f"vector longer than commitment key: {n} > {ck.n}")

    # partial products b_1..b_n; the witness must actually satisfy b_n == b
    partials: List[Scalar] = [a[0]]
    for i in range(1, n):
        partials.append(R.scalar_mul(partials[-1], a[i]))
    if bytes(partials[-1]) != bytes(b):
        raise ValueError("witness does not satisfy prod(a) == b")

    d = [R.random_scalar() for _ in range(n)]
    r_d = R.random_scalar()

    delta: List[Scalar] = [Scalar(bytes(d[0]))]           # delta_1 = d_1
    for _ in range(n - 2):
        delta.append(R.random_scalar())                    # delta_2..delta_{n-1}
    delta.append(Scalar(b"\x00" * 32))                     # delta_n = 0

    s_1 = R.random_scalar()
    s_x = R.random_scalar()

    c_d = commit(ck, d, r_d)

    # c_delta entries: -delta_i * d_{i+1}   for i = 1..n-1  (0-based: i, i+1)
    neg_dd = [R.scalar_negate(R.scalar_mul(delta[i], d[i + 1]))
              for i in range(n - 1)]
    c_delta = commit(ck, neg_dd, s_1)

    # c_Delta entries: delta_{i+1} - a_{i+1}*delta_i - b_i*d_{i+1}
    big = [
        R.scalar_sub(
            R.scalar_sub(delta[i + 1], R.scalar_mul(a[i + 1], delta[i])),
            R.scalar_mul(partials[i], d[i + 1]),
        )
        for i in range(n - 1)
    ]
    c_Delta = commit(ck, big, s_x)

    x = _challenge(ck, commit(ck, a, r), n, b, c_d, c_delta, c_Delta)

    a_tilde = [R.scalar_add(R.scalar_mul(x, a[i]), d[i]) for i in range(n)]
    r_tilde = R.scalar_add(R.scalar_mul(x, r), r_d)
    b_tilde = [R.scalar_add(R.scalar_mul(x, partials[i]), delta[i])
               for i in range(n)]
    s_tilde = R.scalar_add(R.scalar_mul(x, s_x), s_1)

    return SVPProof(c_d=c_d, c_delta=c_delta, c_Delta=c_Delta,
                    a_tilde=a_tilde, b_tilde=b_tilde,
                    r_tilde=r_tilde, s_tilde=s_tilde)


def verify(ck: CommitmentKey, c_a: Point, n: int, b: Scalar,
           proof: SVPProof) -> bool:
    """Verify a single-value product proof for statement (c_a, n, b)."""
    if n < 2 or n > ck.n:
        return False
    if len(proof.a_tilde) != n or len(proof.b_tilde) != n:
        return False

    try:
        x = _challenge(ck, c_a, n, b, proof.c_d, proof.c_delta, proof.c_Delta)
    except ValueError:
        return False

    # (1)  x*c_a + c_d == comck(a~; r~)
    lhs1 = R.add(R.mul_safe(x, c_a), proof.c_d)
    if lhs1 != commit(ck, proof.a_tilde, proof.r_tilde):
        return False

    # (2)  x*c_Delta + c_delta == comck(x*b~_{i+1} - b~_i*a~_{i+1}; s~)
    entries = [
        R.scalar_sub(
            R.scalar_mul(x, proof.b_tilde[i + 1]),
            R.scalar_mul(proof.b_tilde[i], proof.a_tilde[i + 1]),
        )
        for i in range(n - 1)
    ]
    lhs2 = R.add(R.mul_safe(x, proof.c_Delta), proof.c_delta)
    if lhs2 != commit(ck, entries, proof.s_tilde):
        return False

    # (3)  b~_1 == a~_1
    if bytes(proof.b_tilde[0]) != bytes(proof.a_tilde[0]):
        return False

    # (4)  b~_n == x*b
    if bytes(proof.b_tilde[n - 1]) != bytes(R.scalar_mul(x, b)):
        return False

    return True


__all__ = ["SVPProof", "prove", "verify"]
