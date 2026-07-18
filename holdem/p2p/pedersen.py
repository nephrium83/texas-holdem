"""Generalized Pedersen vector commitment over Ristretto255 (Bayer-Groth L4b).

Implements section 2.3 of Bayer & Groth, "Efficient Zero-Knowledge Argument
for Correctness of a Shuffle" (EUROCRYPT 2012): a length-reducing
homomorphic commitment to a vector of up to n field elements as a SINGLE
group element.

    comck(a_1, ..., a_n; r) = r*H + sum_i a_i*G_i        (additive notation)

The paper writes this multiplicatively as H^r * prod G_i^{a_i}; Ristretto255
is written additively, so scalar-mult replaces exponentiation and point-add
replaces multiplication. Committing to fewer than n values sets the rest to
zero.

WHY THE GENERATORS ARE NOTHING-UP-MY-SLEEVE
-------------------------------------------
The soundness of every Bayer-Groth argument rests on this commitment being
*binding*, which in turn requires that NOBODY knows the discrete-log
relations among the generators H, G_1, ..., G_n (the "trapdoor"). This is
exactly what broke the Scytl / Swiss Post e-voting implementation in 2019:
their commitment parameters were generated randomly with no proof of how
they arose, so whoever generated them could know the trapdoor and forge
shuffle proofs that add, drop, or substitute ciphertexts undetectably.

We avoid that entire class of attack by deriving every generator by
hashing a public, domain-separated seed to the group (RFC 9380
hash-to-ristretto255). Anyone can recompute the generators from the seed
and verify they were not chosen with a known trapdoor; there is no secret
setup and no trusted party.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import List, Sequence

from holdem.p2p import ristretto as R
from holdem.p2p.ristretto import Point, Scalar


_GEN_DOMAIN = b"poker.bg.pedersen.gen.v1"


def _derive_generator(seed: bytes, label: bytes, index: int) -> Point:
    """One NUMS generator: hash_to_group(SHA-512(domain|seed|label|index))."""
    h = hashlib.sha512(
        _GEN_DOMAIN + b"|" + seed + b"|" + label + b"|"
        + index.to_bytes(4, "big")
    ).digest()
    return R.hash_to_group(h)


@dataclass(frozen=True)
class CommitmentKey:
    """Public commitment key: blinding generator H and message generators Gs.

    Holds up to ``n`` message generators; a commitment may use fewer. All
    are derived by hashing ``seed`` to the group, so the key is fully
    transparent -- ``regenerate`` reproduces it from the seed alone, and
    ``verify_nums`` confirms that is how these points arose.
    """
    H: Point
    Gs: List[Point]
    seed: bytes

    @property
    def n(self) -> int:
        return len(self.Gs)

    @staticmethod
    def generate(n: int, seed: bytes = b"default") -> "CommitmentKey":
        """Derive a commitment key for vectors of up to ``n`` elements."""
        if n < 1:
            raise ValueError("commitment key needs at least one generator")
        H = _derive_generator(seed, b"H", 0)
        Gs = [_derive_generator(seed, b"G", i) for i in range(n)]
        return CommitmentKey(H=H, Gs=Gs, seed=seed)

    def verify_nums(self) -> bool:
        """Recompute every generator from the seed and confirm it matches.

        A verifier calls this to be sure the committer did not substitute a
        trapdoored generator for a legitimately-hashed one.
        """
        if self.H != _derive_generator(self.seed, b"H", 0):
            return False
        for i, Gi in enumerate(self.Gs):
            if Gi != _derive_generator(self.seed, b"G", i):
                return False
        return True


def commit(ck: CommitmentKey, values: Sequence[Scalar], r: Scalar) -> Point:
    """comck(values; r) = r*H + sum_i values_i * G_i.

    ``values`` may be shorter than ``ck.n`` (the remaining generators are
    treated as multiplied by zero). Raises if it is longer.
    """
    if len(values) > ck.n:
        raise ValueError(f"too many values: {len(values)} > n={ck.n}")
    scalars = [r, *values]
    points = [ck.H, *ck.Gs[: len(values)]]
    return R.multiscalar_mul(scalars, points)


def commit_zero(ck: CommitmentKey, r: Scalar) -> Point:
    """A commitment to the all-zero vector: just r*H."""
    return R.mul_safe(r, ck.H)


__all__ = [
    "CommitmentKey",
    "commit",
    "commit_zero",
]
