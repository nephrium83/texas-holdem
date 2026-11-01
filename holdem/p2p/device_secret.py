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

Storage
-------
The secret is stored through ``identity_storage`` -- the same strict,
object-bound storage as the identity it sits next to (finding 210). It is
read only from a verified, pinned local object; it is created owner-only
(0600 / a protected token-user DACL) before the secret is written; and it
is published by atomic replacement through the pinned directory under an
exclusive lock. Storage that cannot establish that boundary -- reparse
points, substituted roots, short-name aliases, unverifiable volumes, or
ancestors other accounts can mutate -- is refused with InsecureKeyStorage
rather than used. Unsafe EXISTING files are refused, never repaired or
rewritten: a secret another account could have written cannot be trusted
to be ours, and code cannot tell a tampered file from a legitimate one.
The refusal leaves the bytes untouched and tells the operator to restrict
the permissions in place -- which preserves the secret -- not to delete it.

Secrets written by the previous code keep their bytes: on Linux it already
chmod-ed them 0600, so they read back unchanged; on Windows they carry an
inherited DACL and are refused until their permissions are restricted.

This replaced private_storage, which checked nothing about the existing
file and only best-effort chmod-ed a new one (a no-op for ACLs on Windows).

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
from holdem.p2p import identity_storage

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
    target = Path(path) if path is not None else secret_path()
    try:
        # Fast path: an existing secret needs only a verified read -- no
        # lock and no write access (the common case on every launch).
        existing = _read(target)
        if existing is not None:
            return existing
        # Create under the storage lock. Re-read inside it: a concurrent
        # first launch may have published while we waited, and BOTH callers
        # must end up with that one secret or their key shares diverge.
        with identity_storage.creating(target) as publisher:
            existing = _check(target, _absent_is_none(publisher.read_existing))
            if existing is not None:
                return existing
            secret = os.urandom(SECRET_BYTES)
            publisher.publish(secret)
            return secret
    except identity_storage.StorageCleanupError as exc:
        raise ValueError(
            f"cannot store device secret at {target}; release retained storage "
            f"resources before retrying") from exc
    except identity_storage.InsecureKeyStorage as exc:
        raise identity_storage.InsecureKeyStorage(_refusal(target, exc)) from exc


def _refusal(target: Path, cause: Exception) -> str:
    """Refusal text whose remedy PRESERVES the existing secret's bytes.

    An existing secret is never repaired or rewritten automatically: code
    cannot tell a tampered file from a legitimate one, and silently changing
    this device's secret is the failure this module exists to prevent. The
    operator restricts the permissions in place instead, which keeps the
    exact bytes.
    """
    return (
        f"{cause}. The device secret at {target} was not modified, replaced "
        f"or regenerated. Do not delete it: that would change this device's "
        f"key shares. Instead restrict the file and its folders to your "
        f"account and retry -- on Linux: chmod 600 on the file and 700 on its "
        f"folder; on Windows, in PowerShell as this account: "
        f"{windows_remedy(target)} "
        f"(and make sure no ancestor folder grants other accounts write, "
        f"delete or permission-change rights).")


def windows_remedy(target: Path) -> str:
    """One PowerShell command that gives ``target`` exactly the security
    descriptor identity_storage creates -- owned by the current user, a
    protected DACL, the current user its only entry -- keeping the bytes.

    It REPLACES the descriptor rather than editing it: a file created by an
    elevated process can carry explicit (not inherited) Administrators and
    SYSTEM entries, which removing inheritance alone leaves in place.
    """
    path = str(target).replace("'", "''")
    return ("$s=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
            f"$a=Get-Acl -LiteralPath '{path}'; "
            "$a.SetSecurityDescriptorSddlForm(\"O:$($s)D:P(A;;FA;;;$($s))\"); "
            f"Set-Acl -LiteralPath '{path}' -AclObject $a")


def _absent_is_none(read):
    """Run a storage read; a missing file (or directory) means 'no secret'.

    InsecureKeyStorage is a ValueError and propagates unchanged: unsafe
    storage is refused, not treated as absent. Any other OSError is a read
    failure on a secret that may well exist, and is reported as such.
    """
    try:
        return read()
    except FileNotFoundError:
        return None


def _read(target: Path) -> Optional[bytes]:
    """The stored secret, or None if absent. Raises if present but wrong,
    or if the storage itself cannot be trusted."""
    try:
        data = _absent_is_none(lambda: identity_storage.load_identity_bytes(target))
    except identity_storage.InsecureKeyStorage:
        raise
    except OSError as exc:
        raise ValueError(f"cannot read device secret at {target}: {exc}") from exc
    return _check(target, data)


def _check(target: Path, data: Optional[bytes]) -> Optional[bytes]:
    """Validate a stored secret's length; None passes through as absent."""
    if data is None:
        return None
    if len(data) != SECRET_BYTES:
        raise ValueError(
            f"device secret at {target} is {len(data)} bytes, expected "
            f"{SECRET_BYTES}. It will not be replaced automatically, because "
            f"silently changing this device's key shares is the failure this "
            f"file exists to prevent. Delete it to generate a new one (this "
            f"invalidates key shares from earlier hands).")
    return data


__all__ = ["SECRET_BYTES", "FILENAME", "secret_path", "load_or_create"]
