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


def verify(pubkey_bytes: bytes, message: bytes, signature: bytes) -> bool:
    """Verify an Ed25519 signature, returning False for invalid input."""
    try:
        Ed25519PublicKey.from_public_bytes(pubkey_bytes).verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False
