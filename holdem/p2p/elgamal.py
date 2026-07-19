"""ElGamal over Ristretto255 and the canonical 52-card deck encoding.

This is Phase 2 section 1 of docs/MULTIPLAYER.md: the layer directly
above holdem/p2p/ristretto.py. It provides

  * the fixed, public mapping from the 52 cards to group elements, and
  * threshold ElGamal: encrypt, re-encrypt (homomorphic re-randomise),
    partial-decrypt, and combine.

Nothing here is secret or per-hand except the random scalars generated
inside encrypt / re-encrypt. The card->point table is invariant across
all hands and sessions and may be published as a test vector.

Card labels are the canonical two-character rank+suit strings used
throughout the crypto layer (e.g. "As", "2c"). The mapping to the game
engine's own Card objects lives elsewhere; this module deals only in the
canonical labels so the encoding is self-contained and auditable.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import List, Sequence

from holdem.p2p import ristretto as R
from holdem.p2p.ristretto import Point, Scalar


# --------------------------------------------------------------------------
# Canonical deck
# --------------------------------------------------------------------------

SUITS = "cdhs"                 # clubs, diamonds, hearts, spades
RANKS = "23456789TJQKA"
# Canonical order: index 0 = "2c", 1 = "3c", ..., 13 = "2d", ..., 51 = "As".
CARDS: List[str] = [r + s for s in SUITS for r in RANKS]
assert len(CARDS) == 52 and len(set(CARDS)) == 52

_CARD_INDEX = {c: i for i, c in enumerate(CARDS)}


def card_label_bytes(card: str) -> bytes:
    """The 64-byte hash-to-group input for a card, per the deck-encoding spec.

    The spec's domain-separated label is ``poker.card.v1:<idx>:<card>``.
    hash_to_ristretto255 (RFC 9380) consumes 64 uniform bytes, so the label
    is expanded through SHA-512 first. This is deterministic and public.
    """
    idx = _CARD_INDEX[card]
    label = f"poker.card.v1:{idx}:{card}".encode()
    return hashlib.sha512(label).digest()


def card_point(card: str) -> Point:
    """The canonical Ristretto255 point for a card label."""
    return _CARD_POINTS[_CARD_INDEX[card]]


# Precomputed once: the 52 card points, in canonical order. No known
# discrete-log relation to G or to each other (hash-to-curve output).
_CARD_POINTS: List[Point] = [
    R.hash_to_group(card_label_bytes(c)) for c in CARDS
]

# Reverse lookup: point -> card label, for decrypting a dealt ciphertext.
_POINT_TO_CARD = {bytes(p): CARDS[i] for i, p in enumerate(_CARD_POINTS)}


def point_to_card(p: Point) -> str | None:
    """Recover a card label from its point, or None if it is not a card.

    A None result during a real deal means the deck was maliciously
    constructed -- some ciphertext decrypted to a point that is not one of
    the 52 canonical card points.
    """
    return _POINT_TO_CARD.get(bytes(p))


def deck_points() -> List[Point]:
    """The 52 canonical card points, in canonical order (a fresh list)."""
    return list(_CARD_POINTS)


# --------------------------------------------------------------------------
# ElGamal ciphertext
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Ciphertext:
    """An ElGamal ciphertext (C0, C1) under a joint public key.

    C0 = r*G carries the ephemeral randomness; C1 = M + r*PK hides the
    message point M. Both are validated Ristretto255 points.
    """
    c0: Point
    c1: Point

    def to_hex(self) -> tuple[str, str]:
        return (self.c0.hex(), self.c1.hex())

    @staticmethod
    def from_hex(pair: Sequence[str]) -> "Ciphertext":
        """Parse a wire pair [C0_hex, C1_hex], validating both points."""
        c0 = R.point_from_bytes(bytes.fromhex(pair[0]))
        c1 = R.point_from_bytes(bytes.fromhex(pair[1]))
        return Ciphertext(c0, c1)


def encrypt(pk: Point, m: Point, r: Scalar | None = None) -> Ciphertext:
    """Encrypt message point ``m`` under joint public key ``pk``.

    C0 = r*G, C1 = m + r*PK. A fresh random ``r`` is generated unless one
    is supplied (supplying it is for testing / known-answer vectors only).
    """
    if r is None:
        r = R.random_scalar()
    c0 = R.mul_base(r)
    c1 = R.add(m, R.mul(r, pk))
    return Ciphertext(c0, c1)


def reencrypt(pk: Point, ct: Ciphertext, r: Scalar | None = None) -> Ciphertext:
    """Homomorphically re-randomise a ciphertext without changing plaintext.

    (C0, C1) -> (C0 + r'*G, C1 + r'*PK). This is the core move of a shuffle
    round: the plaintext point is unchanged, but the ciphertext is
    unlinkable to its input without knowing r'.
    """
    if r is None:
        r = R.random_scalar()
    c0 = R.add(ct.c0, R.mul_base(r))
    c1 = R.add(ct.c1, R.mul(r, pk))
    return Ciphertext(c0, c1)


def partial_decrypt(ct: Ciphertext, x_share: Scalar) -> Point:
    """Seat i's partial decryption share D_i = x_i * C0.

    Zero-safe: for a trivial ciphertext (C0 = identity) the share is the
    identity, so trivial ciphertexts decrypt correctly via ``combine``.
    """
    return R.mul_safe(x_share, ct.c0)


def combine(ct: Ciphertext, shares: Sequence[Point]) -> Point:
    """Recover the plaintext point: M = C1 - sum(shares).

    ``shares`` must be the partial decryptions D_i = x_i*C0 from every seat
    holding a share of the joint key (including, for a hole card, the
    recipient's own). Subtraction is group subtraction.
    """
    if not shares:
        raise ValueError("combine requires at least one decryption share")
    total = shares[0]
    for s in shares[1:]:
        total = R.add(total, s)
    return R.sub(ct.c1, total)


def make_initial_deck(pk: Point) -> List[Ciphertext]:
    """Encrypt each of the 52 canonical card points under ``pk``.

    TESTS AND UTILITIES ONLY -- NOT the protocol's round-0 deck. This
    uses fresh SECRET randomness, so no other peer can verify what was
    encrypted: a malicious seat 0 could encrypt 52 aces and every
    downstream shuffle (and shuffle proof) would faithfully certify a
    permutation of a corrupt deck. The protocol's shuffle chain MUST
    start from ``make_trivial_deck`` (verifiable by inspection); the
    first shuffler's re-encryption is what introduces secrecy.
    """
    return [encrypt(pk, p) for p in _CARD_POINTS]


def make_trivial_deck() -> List[Ciphertext]:
    """The canonical round-0 deck: trivial encryptions, one per card.

    A trivial encryption uses randomness zero: E(M; 0) = (0*G, M + 0*PK)
    = (identity, M). It is independent of the public key, deterministic,
    and every peer can verify it by inspection (``verify_trivial_deck``),
    so the shuffle chain provably starts from exactly the 52 canonical
    cards. The first re-encrypting shuffler turns these into real
    ciphertexts; until then the "plaintexts" are public by construction,
    which is fine -- the deck order is also public until shuffled.
    """
    return [Ciphertext(R.IDENTITY, p) for p in _CARD_POINTS]


def verify_trivial_deck(deck: Sequence[Ciphertext]) -> bool:
    """Check that ``deck`` is exactly the canonical trivial deck.

    Every peer runs this on the round-0 deck before accepting any
    shuffle built on it. True iff there are 52 entries, each with
    C0 = identity and C1 = the canonical card point for that position.
    """
    if len(deck) != len(_CARD_POINTS):
        return False
    for ct, m in zip(deck, _CARD_POINTS):
        if bytes(ct.c0) != bytes(R.IDENTITY) or bytes(ct.c1) != bytes(m):
            return False
    return True


def joint_public_key(public_shares: Sequence[Point]) -> Point:
    """Combine per-seat public shares X_i = x_i*G into PK = sum(X_i).

    The Phase 1 DKG produces each seat's public share X_i; the joint
    encryption key is their group sum, so that decryption requires every
    corresponding secret share x_i to contribute a partial decryption.
    """
    if not public_shares:
        raise ValueError("joint_public_key requires at least one share")
    pk = public_shares[0]
    for x in public_shares[1:]:
        pk = R.add(pk, x)
    return pk


__all__ = [
    "SUITS", "RANKS", "CARDS",
    "card_label_bytes", "card_point", "point_to_card", "deck_points",
    "Ciphertext",
    "encrypt", "reencrypt", "partial_decrypt", "combine",
    "make_initial_deck", "make_trivial_deck", "verify_trivial_deck",
    "joint_public_key",
]
