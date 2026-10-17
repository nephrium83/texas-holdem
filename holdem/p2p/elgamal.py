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


# Fixed encodings of hash_to_group(card_label_bytes(card)), in CARDS order.
# Loading constants performs no native computation. The native-startup tests
# independently rederive every point and pin the pre-refactor deck digest.
_CARD_POINTS: List[Point] = [Point(bytes.fromhex(value)) for value in (
    "d6f489881d83a4bec8ff6db4e3dd4f3301513f20c88958d974b20e323f02747c",  # 2c
    "7ef1e6adb9161a1bd3a57f4c94cc8b96fd3bfd27cb965f89bcbd46667882466e",  # 3c
    "4c35689373a45d68c88c234682f8128b9c79f533d9bcebd756b38a31a7f01c76",  # 4c
    "7e1a66e803ef5ba3d3661aa44a4d38fa233a519b6a7416dde840ccb373186925",  # 5c
    "9660c40034ece33a1c03b8b21760b584cf5dc6b3d0c0860e6fd1c5066aec761a",  # 6c
    "5492ab73da4939258ee4235979249b4ce3107c65c8e65f9c425d0c07419ede42",  # 7c
    "fa55dd3cfd6a5d52ff2d08a56ff0471f68f82b551f80e0fa2c61c42ef2ed2467",  # 8c
    "a2e325dec96a1e2f36cfd3f413a1df3b9bf424c882b5289ed991a01183a77945",  # 9c
    "3e13c2f33592b29b5a9eef2c969fb327231bd71ac0d5f58e7c4ee4dace98634d",  # Tc
    "641b07bedfea77876eff16f0cc9a5ecc69386f4f80e79e7887df3494ab778242",  # Jc
    "c03590cf9b0bbc63c3c61046294d31994d7de5e2b5952a19ad2563da40884462",  # Qc
    "66e9385cdc2bf2e0b1eaa48a3ea10c19b5d9eb04c6caff74482a9fe45f8feb10",  # Kc
    "7ee6802bddca5cc7464e81df167485607b6caf949590d0e7dbadc2e5a6cb5b60",  # Ac
    "8aea08fb7dc7903cb702a2ec452a4804e8dbe1a4860bc34a145a241891ae7939",  # 2d
    "f6389324e32ab3e38d9aa2dcc080df63e4c879028d98dbec56f6d77971c48401",  # 3d
    "2e3cc871b4732983ebbfb47ceca0b7aeef67cbe5f6d53ea73de60fbf3da19452",  # 4d
    "5a899b14b50f8a6a0d6c9b5352ced4b42ac5f7d69f31460ded2f45835adb733c",  # 5d
    "54345b59b6b12d1c94ee63cf4eea2c02b17f307a6625eb0ecc5427b5ac5df06c",  # 6d
    "82ecfe66ff2d8fb34877849eb943d705b2a5badee284cfa080909cde43ab3620",  # 7d
    "88ea67f275fc5d61cc651843d2ad80fdeceb478433719d10d2ee40323ddcfa5e",  # 8d
    "ba44742e5a782aae4a175746dc17b66b070f2a6a77b33b92709e67c8228b1d4a",  # 9d
    "1c2a313120f70e0cb7005a668da4ca4d3980ed1fd26da00d4f8ac6159c1ffe1f",  # Td
    "fe2f45cdf6836dce3974b9b5808bc99083a88fb1c23d39514a166131fbf99a2b",  # Jd
    "5ada309cb8eeae9fad1aa7907f4c550c7d9aae77f2351f7c12ab3b643a37eb0d",  # Qd
    "9cbf3e0d8a7347f5aaa60a6849f548a9bd30e07422277213287280d3607ffc70",  # Kd
    "7cbeea81ccfee1e1dc91ad73344aafaac4e10939f61dc78eb7d174ff1f990940",  # Ad
    "a6349315cd84b1a0cedb2b0466f6b3f47010636aa71d49df1a42c199808a673c",  # 2h
    "4c2157447145e8d2fd76e670c065a43fda1c923bc2eb3a42ce84b5e9c4e8995c",  # 3h
    "4c0109a29f04edd93b99c725a7bbae065d8840ee6ca648512ae34911f67c9003",  # 4h
    "5ea18f3e3a1cf051a220884e0ca9a745bdd22d1283dfdb5588cc40f92c39b850",  # 5h
    "529c44040809471e52191bc1ef6f8242b98e6ac176eb1dc5c9018f03872f574a",  # 6h
    "e6ce6e2e7d2d6917bb4d69e2e3552557063fbe8e8876e1da6c0363b15ca0c412",  # 7h
    "aeb09a6fa097b2f13a15e95d5c7f5d862daa3d07b487431ace2e978bca1a0663",  # 8h
    "a45ee24bab134b05bc55d31a8ac1342904f80fc277370f786ddb1b9c4ec93b02",  # 9h
    "bc291d3430fe98cb642a4010ee61b53c3ce64b3a5bd77245466390662a9a3057",  # Th
    "745158ce34cf95ce8ab1d2c805b0bd22423243e17c8b8dbd6a0970c5b14aeb09",  # Jh
    "a8805e4bb9680fb40734740dc0c7ecb73fca9355defa9b560f45ed7c9bf5c236",  # Qh
    "76870aee5b2cd7199a60ee74017d2b2104616ce37a82c22201dabe13bed26d09",  # Kh
    "529b7213ba7ca8e10b54409a64db715a0541baf83bc734e31666dbc53b71fa1e",  # Ah
    "46ec3d6313bf26df894a3b092cefe3a8ffc937256f76805cc91261a4db7dba70",  # 2s
    "5623fcc1a952541e0c39e8d8abe2a3ebd3f394cff967e64ac5c6df035249334c",  # 3s
    "f2516ce6636a8c8292144d76f2faae24d058280bb9cd09dc83058619ce7d6419",  # 4s
    "cac1c27b53894d3c9f0e62a596fcbfb250df76c15890ea45a58afe610b8a7451",  # 5s
    "769035c528186dd4729687eb15ed6cd6061e54270cab52b9d432181bc4bd863a",  # 6s
    "789639bc8cf5a543223b8016c054f74b87867a8f0ea46527756e547c34f53807",  # 7s
    "a4d64c1c84a1bf695d011cafe2971df5aa6fe96e073b280cd2a1aedb21b44352",  # 8s
    "3c2b2dda49f35cfa3c56b1182b39e8072c892d3186fbdeffcfb321c811293e49",  # 9s
    "eed6df6d9c73a51ca1a51917c71dc6a1c27c7e0187d39750a113debb0913ee11",  # Ts
    "d6ca46036ec005c267594d0b6272c39d9485e3cff11d3233f97da28147422b1c",  # Js
    "601fa4d2017da335f3f9434ea3b9a958ae2910045907a74b4df56d2b6e11a613",  # Qs
    "72a7e0251500ac646b625a761988328f44670d4124b5b2353c97a2287f223460",  # Ks
    "7aa8c832ff1a32f20bcd114299dc32ca1b726644436ff1a161770ccb044b682c",  # As
)]

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
