"""Pins the crash-survival guarantee behind deterministic key shares.

mental_deal.derive_share promises that "a crashed/reopened app regenerates
the identical share". That holds only if the master secret outlives the
process. It did not: the session drew a fresh os.urandom(32) per launch, so
a peer that crashed and rejoined announced a DIFFERENT public share for the
same seat, and peers holding the first one aborted the hand with
"announced conflicting key shares".

These tests pin the persistence, the failure mode it fixes, and the refusal
to silently replace a corrupt secret.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crypto_gate import initialize_for_tests, NativeCryptoUnavailable

try:
    initialize_for_tests()
    from holdem.p2p import device_secret
    from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
    from holdem.p2p.mental_deal import derive_share
    from holdem.p2p.session import Session
    from holdem.p2p.timeout import FakeClock
except NativeCryptoUnavailable as exc:
    pytest.skip(f"libsodium unavailable: {exc}", allow_module_level=True)


# ------------------------------------------------------ session wiring

def _session(bus, cid, **kw):
    s = Session(is_host=True, nickname="P", avatar_b64="",
                transport=InMemoryTransport(bus, cid), clock=FakeClock(),
                secret_provider=device_secret.load_or_create, **kw)
    s.local_conn_id = cid
    return s


def test_constructing_a_session_touches_no_filesystem(tmp_path, monkeypatch):
    """The secret loads lazily, so building a Session stays side-effect
    free -- the suite constructs hundreds of them."""
    monkeypatch.setenv("HOLDEM_CONFIG_DIR", str(tmp_path / "cfg"))
    _session(InMemoryBus(), "peer0")
    assert not (tmp_path / "cfg").exists()


def test_session_secret_survives_a_restart(tmp_path, monkeypatch):
    """The regression. Two Sessions standing in for the same device before
    and after a crash must derive the same share for the same seat."""
    monkeypatch.setenv("HOLDEM_CONFIG_DIR", str(tmp_path / "cfg"))
    before = _session(InMemoryBus(), "peer0")._deal_master_secret
    after = _session(InMemoryBus(), "peer0")._deal_master_secret
    assert before == after
    assert derive_share(before, "sess", 3, 1) == derive_share(after, "sess", 3, 1)


def test_different_devices_derive_different_shares(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDEM_CONFIG_DIR", str(tmp_path / "one"))
    one = _session(InMemoryBus(), "peer0")._deal_master_secret
    monkeypatch.setenv("HOLDEM_CONFIG_DIR", str(tmp_path / "two"))
    two = _session(InMemoryBus(), "peer0")._deal_master_secret
    assert one != two


def test_explicit_secret_overrides_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDEM_CONFIG_DIR", str(tmp_path / "cfg"))
    injected = bytes(range(32))
    s = _session(InMemoryBus(), "peer0", master_secret=injected)
    assert s._deal_master_secret == injected
    assert not (tmp_path / "cfg").exists(), "override must not read the file"


def test_secret_can_be_overridden_after_construction(tmp_path, monkeypatch):
    """Several tests assign stable per-seat secrets this way."""
    monkeypatch.setenv("HOLDEM_CONFIG_DIR", str(tmp_path / "cfg"))
    s = _session(InMemoryBus(), "peer0")
    s._deal_master_secret = b"\x07" * 32
    assert s._deal_master_secret == b"\x07" * 32


def test_share_is_stable_per_seat_and_hand(tmp_path, monkeypatch):
    """Persistence must not collapse distinct seats onto one share: the
    seat and hand are part of the derivation, so one device secret still
    yields different shares per seat."""
    monkeypatch.setenv("HOLDEM_CONFIG_DIR", str(tmp_path / "cfg"))
    secret = _session(InMemoryBus(), "peer0")._deal_master_secret
    a = derive_share(secret, "sess", 1, 0)
    assert a != derive_share(secret, "sess", 1, 1)      # seat
    assert a != derive_share(secret, "sess", 2, 0)      # hand
    assert a != derive_share(secret, "other", 1, 0)     # session
