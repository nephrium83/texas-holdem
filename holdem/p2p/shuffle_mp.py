"""Verifiable-shuffle *mechanics* over Ristretto255 (mental-poker L4a).

This is the mechanical half of MULTIPLAYER.md Phase 2 section 2: apply a
secret permutation to a deck of ElGamal ciphertexts and re-encrypt every
entry with fresh randomness. The zero-knowledge *proof* that binds the
output to the input (Bayer-Groth, section 2.2) is a separate layer
(L4b); this module deliberately builds and tests the mechanics first.

A shuffle round takes the previous deck and produces:
  * ``deck``     -- the re-encrypted, permuted ciphertexts (goes on the wire)
  * a ``ShuffleWitness`` holding the secret permutation and the fresh
    re-encryption scalars. These are NEVER transmitted; they are retained
    only so the L4b proof can later demonstrate the round was honest.

Why the mechanics are testable without the proof: in a test we control
the secret keys, so we can cooperatively decrypt both the input and the
output decks and assert the *multiset of card points is preserved* (the
shuffle is a bijection that changes only order and randomness, never the
underlying cards). That is the correctness property; soundness against a
cheating shuffler is what the L4b proof will add.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import List, Sequence

from holdem.p2p import ristretto as R
from holdem.p2p.ristretto import Point, Scalar
from holdem.p2p.elgamal import Ciphertext, reencrypt


DECK_SIZE = 52


@dataclass(frozen=True)
class ShuffleWitness:
    """The secret data a shuffle round must retain for its L4b proof.

    ``perm[i] = src`` means output position ``i`` came from input position
    ``src``. ``scalars[i]`` is the fresh re-encryption scalar applied at
    output position ``i``. Neither is ever sent on the wire.
    """
    perm: List[int]
    scalars: List[Scalar]


def random_permutation(n: int = DECK_SIZE) -> List[int]:
    """A uniformly random permutation of ``range(n)`` using a CSPRNG."""
    p = list(range(n))
    secrets.SystemRandom().shuffle(p)
    return p


def shuffle_deck(
    pk: Point,
    prev: Sequence[Ciphertext],
    perm: List[int] | None = None,
    scalars: List[Scalar] | None = None,
) -> tuple[List[Ciphertext], ShuffleWitness]:
    """Apply a secret permutation and re-encrypt every ciphertext.

    Output position ``i`` is a fresh re-encryption of input position
    ``perm[i]``. ``perm`` and ``scalars`` may be supplied for
    deterministic testing / known-answer vectors; otherwise both are
    generated fresh. Returns the new deck and the secret witness.
    """
    n = len(prev)
    if perm is None:
        perm = random_permutation(n)
    if len(perm) != n or sorted(perm) != list(range(n)):
        raise ValueError("perm must be a permutation of range(len(prev))")

    if scalars is None:
        scalars = [R.random_scalar() for _ in range(n)]
    elif len(scalars) != n:
        raise ValueError("scalars must have one entry per deck position")

    deck: List[Ciphertext] = []
    for i, src in enumerate(perm):
        deck.append(reencrypt(pk, prev[src], scalars[i]))
    return deck, ShuffleWitness(perm=list(perm), scalars=list(scalars))


def apply_permutation(prev: Sequence[Ciphertext], perm: Sequence[int]) -> List[Ciphertext]:
    """Reorder (without re-encrypting) -- output[i] = prev[perm[i]].

    Utility for tests and for reasoning about a witness; the real shuffle
    always re-encrypts, so this is not used on the wire.
    """
    return [prev[src] for src in perm]


def inverse_permutation(perm: Sequence[int]) -> List[int]:
    """The inverse: ``inv[perm[i]] = i``."""
    inv = [0] * len(perm)
    for i, src in enumerate(perm):
        inv[src] = i
    return inv


__all__ = [
    "DECK_SIZE",
    "ShuffleWitness",
    "random_permutation",
    "shuffle_deck",
    "apply_permutation",
    "inverse_permutation",
]
