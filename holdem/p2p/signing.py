"""Signing capabilities and Ed25519 verification without stored identity.

The protocol accepts a signer supplied by its host. Verifying a peer's
signature needs only that peer's public key and never loads local keys.
"""
from __future__ import annotations

from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class Signer(Protocol):
    """The local signing capability needed to create a wire envelope."""

    def public_key_bytes(self) -> bytes:
        """Return the signer's raw 32-byte Ed25519 public key."""
        ...

    def sign(self, message: bytes) -> bytes:
        """Return an Ed25519 signature over ``message``."""
        ...


# Encodings of the small-order points of edwards25519 (the 8-torsion
# subgroup), including the non-canonical encodings p and p+1 -- exactly the
# blocklist libsodium's ge25519_has_small_order() uses, compared with the
# x-sign bit cleared.
_P = 2**255 - 19
_SMALL_ORDER = frozenset(bytes.fromhex(h) for h in (
    "0000000000000000000000000000000000000000000000000000000000000000",  # order 4
    "0100000000000000000000000000000000000000000000000000000000000000",  # identity
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",  # order 8
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",  # order 8
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",  # p-1, order 2
    "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",  # p (=0)
    "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",  # p+1 (=1)
))


def _has_small_order(encoding: bytes) -> bool:
    masked = encoding[:31] + bytes([encoding[31] & 0x7F])
    return masked in _SMALL_ORDER


def _is_canonical_point(encoding: bytes) -> bool:
    return int.from_bytes(encoding, "little") & ((1 << 255) - 1) < _P


def verify(pubkey_bytes: bytes, message: bytes, signature: bytes) -> bool:
    """Verify an Ed25519 signature, returning False for invalid input.

    Small-order and non-canonical public keys, and small-order signature
    nonces R, are refused BEFORE the library check (finding N1). OpenSSL's
    Ed25519 (used by ``cryptography``) accepts them: with the identity point
    as the public key, the signature R=identity, S=0 verified for EVERY
    message -- a "signature" needing no private key. Such a key cannot
    impersonate an honest player, but it is an identity nobody can be held
    to. These are the same checks libsodium's crypto_sign_verify_detached
    makes (it also requires S < L, which OpenSSL already enforces).
    """
    if (not isinstance(pubkey_bytes, (bytes, bytearray)) or len(pubkey_bytes) != 32
            or not isinstance(signature, (bytes, bytearray)) or len(signature) != 64):
        return False
    pubkey_bytes, signature = bytes(pubkey_bytes), bytes(signature)
    if (not _is_canonical_point(pubkey_bytes) or _has_small_order(pubkey_bytes)
            or _has_small_order(signature[:32])):
        return False
    try:
        Ed25519PublicKey.from_public_bytes(pubkey_bytes).verify(signature, message)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
