"""Bayer-Groth matrix-elements product argument (paper section 5.2).

This is the thin Theorem 8 composition of sections 5.1 and 5.3.  The
prover commits to the vector of products of the matrix vectors, then proves
both facts about that *same* commitment ``c_b``:

* the vector is the entry-wise product of the committed input vectors; and
* the product of that vector is the public scalar ``b``.

The matrix is represented as ``m`` vectors of width ``n``.  Thus the public
claim is ``b = product(a[i][j])`` over every cell.  ``c_b`` is internal to
the proof and is included in both sub-proofs; it is not supplied by the
verifier as a separate statement commitment.

There is no independent Fiat-Shamir challenge at this wrapper level.  Each
sub-proof remains independently verifiable, so both receive mandatory,
domain-separated contexts that bind the complete wrapper statement and the
shared ``c_b``.  This matters because Theorem 8 deliberately runs the two
arguments against the same commitment.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Sequence

from holdem.p2p import bg_hadamard, bg_svp
from holdem.p2p import ristretto as R
from holdem.p2p.bg_hadamard import HadamardProof
from holdem.p2p.bg_svp import SVPProof
from holdem.p2p.ristretto import Point, Scalar
from holdem.p2p.bg_witness import require_witness
from holdem.p2p.pedersen import CommitmentKey, commit


_DOMAIN = b"poker.bg.product.v1"
_HADAMARD_TAG = b"hadamard"
_SVP_TAG = b"single-value-product"


@dataclass(frozen=True)
class ProductProof:
    """The shared internal commitment and the two Theorem 8 sub-proofs."""

    c_b: Point
    hadamard: HadamardProof
    svp: SVPProof


def _field(h: "hashlib._Hash", data: bytes) -> None:
    h.update(len(data).to_bytes(4, "big"))
    h.update(data)


def _point_list(h: "hashlib._Hash", points: Sequence[Point]) -> None:
    h.update(len(points).to_bytes(4, "big"))
    for point in points:
        _field(h, bytes(point))


def _child_context(context: bytes, tag: bytes, ck: CommitmentKey,
                  c_A: Sequence[Point], n: int, b: Scalar,
                  c_b: Point) -> bytes:
    """Derive a distinct, length-prefixed context for each sub-proof."""
    h = hashlib.sha512()
    _field(h, _DOMAIN)
    _field(h, tag)
    _field(h, context)
    _field(h, bytes(ck.H))
    _point_list(h, ck.Gs)
    h.update(len(c_A).to_bytes(4, "big"))
    h.update(n.to_bytes(4, "big"))
    _point_list(h, c_A)
    _field(h, bytes(b))
    _field(h, bytes(c_b))
    return h.digest()


def _validate_shape(ck: CommitmentKey, c_A: Sequence[Point],
                    a: Sequence[Sequence[Scalar]], r: Sequence[Scalar] | None,
                    n: int, *, check_vectors: bool = True) -> int:
    m = len(c_A)
    if m < 2:
        raise ValueError("product argument requires m >= 2")
    if n < 2:
        raise ValueError("product argument requires n >= 2")
    if n > ck.n:
        raise ValueError("product argument vector width exceeds commitment key")
    if len(a) != m:
        raise ValueError("c_A and matrix must have the same number of vectors")
    if r is not None and len(r) != m:
        raise ValueError("c_A, matrix and openings must have length m")
    if check_vectors:
        for vector in a:
            if len(vector) != n:
                raise ValueError("every matrix vector must have width n")
    return m


def _product(vector: Sequence[Scalar]) -> Scalar:
    value = Scalar(b"\x01" + b"\x00" * 31)
    for element in vector:
        value = R.scalar_mul(value, element)
    return value


def prove(ck: CommitmentKey, c_A: Sequence[Point],
          a: Sequence[Sequence[Scalar]], r: Sequence[Scalar],
          b: Scalar, context: bytes) -> ProductProof:
    """Prove that the committed matrix entries multiply to public ``b``."""
    if not a:
        raise ValueError("product argument requires a non-empty matrix")
    n = len(a[0])
    m = _validate_shape(ck, c_A, a, r, n)

    for i in range(m):
        if commit(ck, a[i], r[i]) != c_A[i]:
            raise ValueError(f"c_A[{i}] does not open to the supplied vector")

    row_products = [list(a[0])]
    for i in range(1, m):
        row_products.append([
            R.scalar_mul(row_products[-1][j], a[i][j])
            for j in range(n)
        ])
    require_witness(
        bytes(_product(row_products[-1])) == bytes(b),
        "witness does not satisfy product of all matrix entries")

    s_b = R.random_scalar()
    c_b = commit(ck, row_products[-1], s_b)
    hadamard_context = _child_context(
        context, _HADAMARD_TAG, ck, c_A, n, b, c_b)
    svp_context = _child_context(context, _SVP_TAG, ck, c_A, n, b, c_b)

    hadamard = bg_hadamard.prove(
        ck, c_A, a, r, c_b, row_products[-1], s_b, hadamard_context)
    svp = bg_svp.prove(ck, row_products[-1], s_b, b, svp_context)
    return ProductProof(c_b=c_b, hadamard=hadamard, svp=svp)


def verify(ck: CommitmentKey, c_A: Sequence[Point], n: int, b: Scalar,
           context: bytes, proof: ProductProof) -> bool:
    """Verify a Theorem 8 product proof. Returns False; never raises."""
    try:
        # Called for its validation side effect; the returned m is unused.
        _validate_shape(
            ck, c_A, [[] for _ in range(len(c_A))], None, n,
            check_vectors=False)
        hadamard_context = _child_context(
            context, _HADAMARD_TAG, ck, c_A, n, b, proof.c_b)
        svp_context = _child_context(
            context, _SVP_TAG, ck, c_A, n, b, proof.c_b)
        return (
            bg_hadamard.verify(
                ck, c_A, proof.c_b, n, hadamard_context, proof.hadamard)
            and bg_svp.verify(
                ck, proof.c_b, n, b, svp_context, proof.svp)
        )
    except (AttributeError, IndexError, TypeError, ValueError):
        return False


__all__ = ["ProductProof", "prove", "verify"]
