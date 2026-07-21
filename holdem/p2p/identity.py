"""Ed25519 peer identity — generated once, persisted to disk.

The same keypair serves two roles:
  - Protocol identity: the ``pubkey`` field in every signed action envelope
    and the signing key for all game messages (Phase 1).
  - Transport identity: the peer ID at the network layer (Phase 3).

A companion X25519 keypair is also stored in the same file. It served
hole-card encryption in the retired commit-reveal shuffle and is kept
for future transport-layer encryption; nothing consumes it today.

Generating the keypairs on first launch is the full "setup" the user
performs.  The keypairs are stable across sessions so that a reconnecting
player presents the same peer ID and pubkey the other seats already have
in their logs.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
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


def _load_or_create() -> tuple[Ed25519PrivateKey, X25519PrivateKey]:
    path = _identity_path()
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception:
            data = {}

    changed = False

    # --- Ed25519 key ---
    if "private_key_b64" in data:
        raw_ed = base64.b64decode(data["private_key_b64"])
        ed_key = Ed25519PrivateKey.from_private_bytes(raw_ed)
    else:
        ed_key = Ed25519PrivateKey.generate()
        raw_ed = ed_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        data["private_key_b64"] = base64.b64encode(raw_ed).decode()
        changed = True

    # --- X25519 key (added alongside Ed25519 for hole-card encryption) ---
    if "x25519_private_key_b64" in data:
        raw_x = base64.b64decode(data["x25519_private_key_b64"])
        x25519_key = X25519PrivateKey.from_private_bytes(raw_x)
    else:
        x25519_key = X25519PrivateKey.generate()
        raw_x = x25519_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        data["x25519_private_key_b64"] = base64.b64encode(raw_x).decode()
        changed = True

    if changed:
        path.write_text(json.dumps(data))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass   # Windows: best-effort

    return ed_key, x25519_key


_private_key: Ed25519PrivateKey
_x25519_private_key: X25519PrivateKey
_private_key, _x25519_private_key = _load_or_create()


# ---------------------------------------------------------------------------
# Ed25519 identity (signing / peer-ID)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# X25519 encryption keypair (hole-card encryption in verifiable shuffle)
# ---------------------------------------------------------------------------

def x25519_private_key() -> X25519PrivateKey:
    """Return the process-wide X25519 private key (for hole-card decryption)."""
    return _x25519_private_key


def x25519_public_key_bytes() -> bytes:
    """Return the raw 32-byte X25519 public key (share during shuffle commit)."""
    return _x25519_private_key.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )
