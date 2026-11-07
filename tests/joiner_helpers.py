"""Wire-mode joiners for tests about something else (audit #72).

A wire-mode joiner must be built with its invite's JoinerAdmission, which
pins the host key; Session refuses to construct one without it. Tests that
need a joiner already talking to "the host" get there the legitimate way:
pin the key the test's host messages are really signed with, then
authenticate through Session.mark_host_authenticated -- the same owned entry
point join_auth uses, which checks the offered key AGAINST the pin.

Deliberately not a bypass: nothing here sets _host_authenticated directly,
and a test that pins the wrong key fails exactly as production would.
"""
from __future__ import annotations

from typing import Optional

from holdem.p2p import admission as _adm
from holdem.p2p import identity as _identity

TEST_ADMISSION_SECRET = bytes(_adm.SECRET_LEN)


def process_pubkey() -> bytes:
    """The key this process signs envelopes with (the test's config dir)."""
    return _identity.load_or_create().public_key_bytes()


def joiner_admission(host_pubkey: Optional[bytes] = None,
                     joiner_pubkey: Optional[bytes] = None) -> _adm.JoinerAdmission:
    """An admission pinning ``host_pubkey`` (default: this process's key)."""
    return _adm.JoinerAdmission(
        admission_secret=TEST_ADMISSION_SECRET,
        host_pubkey=bytes(host_pubkey if host_pubkey is not None else process_pubkey()),
        joiner_pubkey=bytes(joiner_pubkey if joiner_pubkey is not None else process_pubkey()))


def authenticate_host(session, host_conn: str,
                      host_pubkey: Optional[bytes] = None) -> None:
    """Authenticate ``host_conn`` as the pinned host via the real entry point."""
    key = bytes(host_pubkey if host_pubkey is not None else process_pubkey())
    if not session.mark_host_authenticated(host_conn, key):
        raise AssertionError(f"session refused {host_conn} as the pinned host")
