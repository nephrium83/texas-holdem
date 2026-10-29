"""Ed25519 peer identity — generated once, persisted to disk.

The same keypair serves two roles:
  - Protocol identity: the ``pubkey`` field in every signed action envelope
    and the signing key for all game messages (Phase 1).
  - Transport identity: the peer ID at the network layer (Phase 3).

A companion X25519 keypair is also stored in the same file. The Internet
setup adapter uses it to encrypt and authenticate connection descriptions.

Importing this module does not read or write any files. The host explicitly
loads its Identity with load_or_create(); legacy module helpers load the
same process default on first use.

Generating the keypairs on first launch is the full "setup" the user
performs.  The keypairs are stable across sessions so that a reconnecting
player presents the same peer ID and pubkey the other seats already have
in their logs.
"""
from __future__ import annotations

import base64
import json
import pathlib
import threading

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from holdem.p2p.signing import verify
from holdem.p2p.private_storage import (
    atomic_write, canonical_target, locked, ensure_private_dir, read_private,
)


def _identity_path() -> pathlib.Path:
    """L-4: Store identity alongside other app config via settings.config_dir()."""
    from holdem.settings import config_dir
    d = config_dir()
    ensure_private_dir(d)
    return d / "identity.json"


def _load_keys(path: pathlib.Path) -> tuple[Ed25519PrivateKey, X25519PrivateKey]:
    path = canonical_target(path)
    data, _ = _read_key_data(path)
    # Complete identities are immutable. Loading one needs only read access,
    # including when supplied by an embedding host on a read-only volume.
    if "x25519_private_key_b64" in data:
        return (Ed25519PrivateKey.from_private_bytes(base64.b64decode(data["private_key_b64"])),
                X25519PrivateKey.from_private_bytes(base64.b64decode(data["x25519_private_key_b64"])))
    with locked(path):
        return _load_keys_locked(path)


def _read_key_data(path: pathlib.Path) -> tuple[dict, int]:
    try:
        private = read_private(path)
        if private is None:
            return {}, 0
        stored = private.data.decode('utf-8')
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read identity at {path}; restore the existing file before retrying") from exc
    else:
        try:
            data = json.loads(stored)
            if not isinstance(data, dict) or "private_key_b64" not in data:
                raise ValueError("missing signing identity")
            for field in ("private_key_b64", "x25519_private_key_b64"):
                if field in data:
                    if not isinstance(data[field], str):
                        raise ValueError("key must be a base64 string")
                    if len(base64.b64decode(data[field], validate=True)) != 32:
                        raise ValueError("key must be 32 bytes")
        except (ValueError, UnicodeError) as exc:
            raise ValueError(f"invalid identity at {path}; restore it from a trusted backup. "
                             "It will not be replaced automatically") from exc
    return data, private.links


def _load_keys_locked(path: pathlib.Path) -> tuple[Ed25519PrivateKey, X25519PrivateKey]:
    data, links = _read_key_data(path)
    if data and "x25519_private_key_b64" not in data and links > 1:
        raise ValueError(f"legacy identity at {path} has multiple hard links; "
                         "use an independent copy before upgrading it")
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
        atomic_write(path, json.dumps(data).encode("utf-8"))

    return ed_key, x25519_key


class Identity:
    """One host-owned identity, independent of the process default."""

    def __init__(self, ed25519_key: Ed25519PrivateKey,
                 x25519_key: X25519PrivateKey) -> None:
        self._private_key = ed25519_key
        self._x25519_private_key = x25519_key

    def private_key(self) -> Ed25519PrivateKey:
        return self._private_key

    def public_key_bytes(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw)

    def peer_id(self) -> str:
        return self.public_key_bytes()[:8].hex()

    def sign(self, message: bytes) -> bytes:
        return self._private_key.sign(message)

    @staticmethod
    def verify(pubkey_bytes: bytes, message: bytes, signature: bytes) -> bool:
        return verify(pubkey_bytes, message, signature)

    def x25519_private_key(self) -> X25519PrivateKey:
        return self._x25519_private_key

    def x25519_public_key_bytes(self) -> bytes:
        return self._x25519_private_key.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw)


_default_identity: Identity | None = None
_default_lock = threading.Lock()


def load_or_create(path: pathlib.Path | str | None = None) -> Identity:
    """Load stored keys, creating them if absent.

    Without a path, cache one identity for this process using the configured
    identity file. An explicit path loads a separate identity, so a host can
    own multiple identities without changing the process default.
    """
    if path is not None:
        return Identity(*_load_keys(pathlib.Path(path)))
    global _default_identity
    with _default_lock:
        if _default_identity is None:
            _default_identity = Identity(*_load_keys(_identity_path()))
        return _default_identity


# ---------------------------------------------------------------------------
# Ed25519 identity (signing / peer-ID)
# ---------------------------------------------------------------------------

def private_key() -> Ed25519PrivateKey:
    """Return the process-wide Ed25519 private key."""
    return load_or_create().private_key()


def public_key_bytes() -> bytes:
    """Return the raw 32-byte Ed25519 public key."""
    return load_or_create().public_key_bytes()


def peer_id() -> str:
    """Hex string of first 8 bytes of public key — used in invite codes."""
    return load_or_create().peer_id()


def sign(message: bytes) -> bytes:
    """Sign *message* with the local private key; returns 64-byte signature."""
    return load_or_create().sign(message)


# ---------------------------------------------------------------------------
# X25519 encryption keypair (hole-card encryption in verifiable shuffle)
# ---------------------------------------------------------------------------

def x25519_private_key() -> X25519PrivateKey:
    """Return the process-wide X25519 private key (for hole-card decryption)."""
    return load_or_create().x25519_private_key()


def x25519_public_key_bytes() -> bytes:
    """Return the raw 32-byte X25519 public key (share during shuffle commit)."""
    return load_or_create().x25519_public_key_bytes()
