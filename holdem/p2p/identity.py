"""Ed25519 peer identity — generated once, persisted to disk.

The same keypair serves two roles:
  - Protocol identity: the ``pubkey`` field in every signed action envelope
    and the signing key for all game messages (Phase 1).
  - Transport identity: the peer ID at the network layer (Phase 3).

Generating the keypair on first launch is the full "setup" the user
performs.  The keypair is stable across sessions so that a reconnecting
player presents the same peer ID and pubkey the other seats already have
in their logs.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


def _identity_path() -> pathlib.Path:
    """L-4: Store identity alongside other app config via settings.config_dir()."""
    from holdem.settings import config_dir
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "identity.json"


def _load_or_create() -> Ed25519PrivateKey:
    path = _identity_path()
    if path.exists():
        data = json.loads(path.read_text())
        raw = base64.b64decode(data["private_key_b64"])
        return Ed25519PrivateKey.from_private_bytes(raw)
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    path.write_text(
        json.dumps({"private_key_b64": base64.b64encode(raw).decode()})
    )
    # M-10: restrict identity file to owner read/write only
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass   # Windows: best-effort
    return key


_private_key: Ed25519PrivateKey = _load_or_create()


def private_key() -> Ed25519PrivateKey:
    """Return the process-wide Ed25519 private key."""
    return _private_key


def public_key_bytes() -> bytes:
    """Return the raw 32-byte Ed25519 public key."""
    return _private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def peer_id() -> str:
    """Hex string of first 8 bytes of public key — used in invite codes."""
    return public_key_bytes()[:8].hex()


def sign(message: bytes) -> bytes:
    """Sign *message* with the local private key; returns 64-byte signature."""
    return _private_key.sign(message)


def verify(pubkey_bytes: bytes, message: bytes, signature: bytes) -> bool:
    """Verify *signature* over *message* using *pubkey_bytes*.

    Returns ``True`` on success, ``False`` on any failure (bad signature,
    wrong key, malformed input).
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(pubkey_bytes).verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False
