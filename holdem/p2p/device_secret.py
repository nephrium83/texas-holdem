"""The per-device secret behind deterministic mental-poker key shares.

``mental_deal.derive_share`` computes x_i = HKDF(master_secret,
session|hand|seat) and documents the consequence: "a crashed/reopened app
regenerates the identical share". That guarantee only holds if the master
secret itself survives the restart. It did not -- the session generated a
fresh ``os.urandom(32)`` per process -- so a peer that crashed and rejoined
derived a DIFFERENT public share for the same seat, and the peers that had
already accepted the first one aborted the hand with "announced conflicting
key shares". The property was documented but not delivered.

This module supplies the missing half: one 32-byte secret per device,
created on first use and reused forever after.

Scope and threat model
----------------------
The secret never leaves the process and is never transmitted; only
X_i = x_i*G goes on the wire. It is stored unencrypted, at rest, in the
same config directory as the rest of the client's state. That is
deliberate and sufficient for a play-money game with no custody: anyone
who can read the file can already read the process memory that holds the
key and the hole cards it decrypts. Encrypting it would require a
passphrase prompt on every launch and protect nothing that is not already
lost. Do not reuse this module for anything of value without revisiting
that decision.

Corruption fails loudly
-----------------------
A file that exists but is not exactly 32 bytes raises rather than being
silently replaced. Silent regeneration is precisely the bug this module
exists to fix -- an identity that changes without anyone noticing -- so it
is not an acceptable recovery. Deleting the file is an explicit,
recoverable action the operator can take; the error message says so.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from holdem.settings import config_dir
from holdem.p2p.private_storage import atomic_write, canonical_target, locked

SECRET_BYTES = 32
FILENAME = "device_secret"


def secret_path() -> Path:
    """Where the device secret lives (honours HOLDEM_CONFIG_DIR)."""
    return config_dir() / FILENAME


def load_or_create(path: Optional[Path] = None) -> bytes:
    """Return this device's 32-byte secret, creating it on first use.

    Concurrency-safe, and specifically safe against a reader arriving
    mid-creation: the secret is written to a temporary file and only then
    published under ``target`` by an atomic replacement while creators hold
    a shared process lock, so the target path is
    never observable at a partial size. Raises ValueError if an existing
    file is not exactly ``SECRET_BYTES`` long.

    The previous version created ``target`` directly with O_EXCL and then
    wrote into it. O_EXCL elects one winner and prevents clobbering, but
    it does not stop anyone from reading the file in the window between
    the winner's open -- which creates it at zero length -- and the
    winner's write. A second process launching at the same moment took
    that window on its very FIRST read, saw 0 bytes, and raised, never
    reaching the O_EXCL path at all.

    That was the intermittent two-process CI stall: the host's
    start_p2p_hand raised "device secret ... is 0 bytes, expected 32",
    the command went unacknowledged, and the hand never began. It only
    bites on a fresh config directory -- which CI has every run and a
    developer machine essentially never has, which is why every observed
    occurrence was on Linux CI and none locally.
    """
    target = canonical_target(Path(path) if path is not None else secret_path())

    existing = _read(target)
    if existing is not None:
        return existing

    with locked(target):
        existing = _read(target)
        if existing is not None:
            return existing
        secret = os.urandom(SECRET_BYTES)
        atomic_write(target, secret)
        _restrict(target)
        return secret


def _read(target: Path) -> Optional[bytes]:
    """The stored secret, or None if absent. Raises if present but wrong."""
    try:
        data = target.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"cannot read device secret at {target}: {exc}") from exc
    if len(data) != SECRET_BYTES:
        raise ValueError(
            f"device secret at {target} is {len(data)} bytes, expected "
            f"{SECRET_BYTES}. It will not be replaced automatically, because "
            f"silently changing this device's key shares is the failure this "
            f"file exists to prevent. Delete it to generate a new one (this "
            f"invalidates key shares from earlier hands).")
    return data


def _restrict(target: Path) -> None:
    """Best-effort owner-only permissions.

    Meaningful on POSIX. On Windows chmod only toggles the read-only
    attribute and does not touch ACLs, so the file inherits the config
    directory's inherited permissions -- acceptable given the threat model
    above, where the secret is no more sensitive than the process memory
    it lives in.
    """
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


__all__ = ["SECRET_BYTES", "FILENAME", "secret_path", "load_or_create"]
