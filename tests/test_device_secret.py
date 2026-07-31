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

try:
    from holdem.p2p import device_secret
    from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
    from holdem.p2p.mental_deal import derive_share
    from holdem.p2p.session import Session
except RuntimeError as exc:
    pytest.skip(f"libsodium unavailable: {exc}", allow_module_level=True)


# ------------------------------------------------------------ the file

def test_created_on_first_use(tmp_path):
    target = tmp_path / "device_secret"
    assert not target.exists()
    secret = device_secret.load_or_create(target)
    assert target.exists()
    assert len(secret) == device_secret.SECRET_BYTES


def test_stable_across_calls(tmp_path):
    """The whole point: a second process must read the first one's secret."""
    target = tmp_path / "device_secret"
    assert device_secret.load_or_create(target) == \
        device_secret.load_or_create(target)


def test_distinct_devices_get_distinct_secrets(tmp_path):
    a = device_secret.load_or_create(tmp_path / "a" / "device_secret")
    b = device_secret.load_or_create(tmp_path / "b" / "device_secret")
    assert a != b


def test_parent_directory_is_created(tmp_path):
    target = tmp_path / "nested" / "deeper" / "device_secret"
    assert len(device_secret.load_or_create(target)) == \
        device_secret.SECRET_BYTES


def test_honours_the_config_dir_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDEM_CONFIG_DIR", str(tmp_path / "cfg"))
    assert device_secret.secret_path() == \
        tmp_path / "cfg" / device_secret.FILENAME


# ------------------------------------------------------- corrupt file

@pytest.mark.parametrize("content", [b"", b"short", b"x" * 31, b"x" * 33])
def test_wrong_length_raises_rather_than_regenerating(tmp_path, content):
    """Silent regeneration is the exact bug this module fixes -- an identity
    changing without anyone noticing -- so it must not be the recovery."""
    target = tmp_path / "device_secret"
    target.write_bytes(content)
    with pytest.raises(ValueError, match="expected 32"):
        device_secret.load_or_create(target)
    assert target.read_bytes() == content, "corrupt file must be left alone"


def test_error_message_tells_the_operator_what_to_do(tmp_path):
    target = tmp_path / "device_secret"
    target.write_bytes(b"nope")
    with pytest.raises(ValueError, match="[Dd]elete it"):
        device_secret.load_or_create(target)


# ------------------------------------------------------ session wiring

def _session(bus, cid, **kw):
    s = Session(is_host=True, nickname="P", avatar_b64="",
                transport=InMemoryTransport(bus, cid), **kw)
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
