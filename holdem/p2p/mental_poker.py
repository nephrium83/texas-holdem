"""Deck encoding and ElGamal over Ristretto255 (mental-poker layer 2).

Implements MULTIPLAYER.md Phase 2 section 1: the fixed 52-card -> point
encoding and the additively-homomorphic ElGamal scheme the shuffle is
built on. This layer has no networking and no proofs -- it is pure
algebra over holdem/p2p/ristretto.py, and every operation here is
covered by round-trip tests.

Card encoding
-------------
Cards are the 52 canonical two-char labels in suit-major order
(``2c 3c ... Ac 2d ...``). Each maps to a group element by

    card_point(card) = hash_to_group( SHA-512( "poker.card.v1:{idx}:{card}" ) )

The SHA-512 step adapts the spec's pseudocode to libsodium's
``crypto_core_ristretto255_from_hash``, which takes 64 uniform bytes and
applies the RFC 9380 hash-to-ristretto255 map. This construction is the
standard way to hash-to-group with libsodium; the 52 resulting points
are fixed for all time, cached at import, and pinned by a test vector so
any divergence between peers is caught immediately.

ElGamal
-------
A ciphertext is a pair of points ``Ciphertext(c0, c1)``. Under a joint
public key ``PK`` (from the Phase 1 DKG) with per-seat secret shares
``x_i`` such that ``PK = (sum x_i) * G``:

    encrypt(M)          = (r*G,  M + r*PK)                 for random r
    reencrypt(C)        = (c0 + r'*G,  c1 + r'*PK)         plaintext unchanged
    partial_decrypt(C)  = x_i * c0                          seat i's share
    full_decrypt(C, Ds) = c1 - sum(Ds)                      = M

Full decryption requires the partial-decrypt shares of every seat whose
key is in PK; that cooperative requirement is what makes selective
dealing (who learns which card) possible in the layers above.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, List, Sequence

from holdem.p2p import ristretto as R
from holdem.p2p.ristretto import Point, Scalar


# --------------------------------------------------------------------------
# Deck encoding
# --------------------------------------------------------------------------

SUITS = "cdhs"                 # clubs, diamonds, hearts, spades
RANKS = "23456789TJQKA"
CARDS: List[str] = [r + s for s in SUITS for r in RANKS]   # suit-major, 52
assert len(CARDS) == 52 and len(set(CARDS)) == 52

_CARD_INDEX = {c: i for i, c in enumerate(CARDS)}


def _encode_card(card: str) -> Point:
    idx = _CARD_INDEX[card]
    label = f"poker.card.v1:{idx}:{card}".encode("utf-8")
    wide = hashlib.sha512(label).digest()          # 64 uniform bytes
    return R.hash_to_group(wide)


# Precompute all 52 card points once. Invariant across hands and sessions.
CARD_POINTS: List[Point] = [_encode_card(c) for c in CARDS]
_POINT_TO_CARD = {bytes(p): CARDS[i] for i, p in enumerate(CARD_POINTS)}


def card_point(card: str) -> Point:
    """The fixed Ristretto255 encoding of a card label like ``'As'``."""
    try:
        return CARD_POINTS[_CARD_INDEX[card]]
    except KeyError:
        raise ValueError(f"not a valid card label: {card!r}")


def point_to_card(p: Point) -> str | None:
    """Reverse lookup: the card whose point this is, or None if unknown.

    A None result during dealing means the recovered plaintext is not any
    card point -- evidence the deck was maliciously constructed.
    """
    return _POINT_TO_CARD.get(bytes(p))


def deck_points() -> List[Point]:
    """A fresh list of the 52 plaintext card points in canonical order."""
    return list(CARD_POINTS)


# --------------------------------------------------------------------------
# ElGamal
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Ciphertext:
    """An ElGamal ciphertext: the pair of Ristretto255 points (c0, c1)."""

    c0: Point
    c1: Point

    def to_hex(self) -> tuple[str, str]:
        return (self.c0.hex(), self.c1.hex())

    @staticmethod
    def from_hex(pair: Sequence[str]) -> "Ciphertext":
        """Parse a wire ``[c0_hex, c1_hex]`` pair, validating both points."""
        c0_hex, c1_hex = pair
        return Ciphertext(
            R.point_from_bytes(bytes.fromhex(c0_hex)),
            R.point_from_bytes(bytes.fromhex(c1_hex)),
        )


def encrypt(pk: Point, m: Point, r: Scalar | None = None) -> Ciphertext:
    """Encrypt plaintext point ``m`` under joint public key ``pk``.

    C = (r*G, m + r*pk). ``r`` may be supplied for testing; otherwise a
    fresh random scalar is used.
    """
    if r is None:
        r = R.random_scalar()
    c0 = R.mul_base(r)
    c1 = R.add(m, R.mul(r, pk))
    return Ciphertext(c0, c1)


def reencrypt(pk: Point, c: Ciphertext, r: Scalar | None = None) -> Ciphertext:
    """Re-randomise ``c`` under ``pk`` without changing its plaintext.

    (c0 + r'*G, c1 + r'*pk). This is the core move of a shuffle round.
    """
    if r is None:
        r = R.random_scalar()
    return Ciphertext(
        R.add(c.c0, R.mul_base(r)),
        R.add(c.c1, R.mul(r, pk)),
    )


def partial_decrypt(x_i: Scalar, c: Ciphertext) -> Point:
    """Seat i's partial-decryption share D_i = x_i * c0."""
    return R.mul(x_i, c.c0)


def combine_shares(shares: Iterable[Point]) -> Point:
    """Sum a non-empty collection of partial-decryption shares."""
    it = iter(shares)
    try:
        total = next(it)
    except StopIteration:
        raise ValueError("combine_shares requires at least one share")
    for s in it:
        total = R.add(total, s)
    return total


def full_decrypt(c: Ciphertext, shares: Sequence[Point]) -> Point:
    """Recover the plaintext point: M = c1 - sum(shares).

    ``shares`` must be the partial decryptions of every seat whose key is
    in the joint public key.
    """
    return R.sub(c.c1, combine_shares(shares))


def joint_public_key(shares_pub: Sequence[Point]) -> Point:
    """Combine per-seat public shares X_i = x_i*G into PK = sum(X_i).

    (The DKG in Phase 1 produces the X_i; this is how the encryption key
    is assembled from them.)
    """
    if not shares_pub:
        raise ValueError("need at least one public share")
    pk = shares_pub[0]
    for x in shares_pub[1:]:
        pk = R.add(pk, x)
    return pk


__all__ = [
    "SUITS", "RANKS", "CARDS", "CARD_POINTS",
    "card_point", "point_to_card", "deck_points",
    "Ciphertext",
    "encrypt", "reencrypt", "partial_decrypt",
    "combine_shares", "full_decrypt", "joint_public_key",
]
