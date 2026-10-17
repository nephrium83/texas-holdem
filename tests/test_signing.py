"""Explicit signers preserve wire bytes and load host identity only on use."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from holdem.p2p import identity, wire
from holdem.p2p.timeout import FakeClock
from holdem.p2p.signing import verify


def _identity(seed: bytes) -> identity.Identity:
    return identity.Identity(Ed25519PrivateKey.from_private_bytes(seed),
                             X25519PrivateKey.from_private_bytes(seed))


# Captured from the pre-refactor envelope algorithm with a fixed Ed25519
# seed and clock. This pins every byte, including JSON ordering and escaping,
# the canonical signature, the previous hash, and the final envelope hash.
_LEGACY_WIRE = (
    b'{"v": 1, "type": "chat", "payload": {"text": "hi \\u2660", "seat": 2}, '
    b'"pubkey": "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8", '
    b'"ts": 1720000000250, '
    b'"prev": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", '
    b'"sig": "be90060d3970308aa23ce95826f0183b1040a23417f0dd3a7519b318c6e988a87'
    b'acd251c913ebd1718be4cf1bdb4bdf3ba39efbd280f36e6bb953f9cbd384200", '
    b'"hash": "8c766c46fcd1aa5697212d00599e31740ba1d44a37bbea778f898b37a28ef9ef"}'
)


def test_explicit_signer_preserves_every_legacy_wire_byte():
    clock = FakeClock(wall_start=1720000000.25)
    signer = _identity(bytes(range(32)))
    raw = wire.pack("chat", {"text": "hi \u2660", "seat": 2}, "a" * 64,
                    signer=signer, clock=clock)
    assert raw == _LEGACY_WIRE
    assert wire.unpack(raw, clock=clock)["payload"] == {"text": "hi \u2660", "seat": 2}


def test_two_signers_in_one_process_keep_distinct_authors():
    first = _identity(bytes(range(32)))
    second = _identity(bytes(reversed(range(32))))
    clock = FakeClock()
    a = wire.unpack(wire.pack("chat", {"text": "one"}, signer=first, clock=clock),
                    clock=clock)
    b = wire.unpack(wire.pack("chat", {"text": "two"}, signer=second, clock=clock),
                    clock=clock)
    assert a["pubkey"] == first.public_key_bytes().hex()
    assert b["pubkey"] == second.public_key_bytes().hex()
    assert a["pubkey"] != b["pubkey"]
    message = b"one author cannot sign for another"
    assert verify(first.public_key_bytes(), message, first.sign(message))
    assert not verify(second.public_key_bytes(), message, first.sign(message))


def test_wire_verification_never_loads_a_local_identity(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("verifying a peer loaded local key material")

    monkeypatch.setattr(identity, "load_or_create", forbidden)
    clock = FakeClock(wall_start=1720000000.25)
    assert wire.unpack(_LEGACY_WIRE, clock=clock)["type"] == "chat"


def test_wire_timestamp_and_replay_window_follow_only_injected_wall_time():
    clock = FakeClock(start=900.0, wall_start=1000.25)
    signer = _identity(bytes(range(32)))
    raw = wire.pack("chat", {"text": "clock"}, signer=signer, clock=clock)
    assert wire.safe_loads(raw)["ts"] == 1000250, (
        "wire.pack bypassed the injected wall clock")
    assert wire.unpack(raw, clock=clock)["type"] == "chat"
    clock.advance_wall(30.0)
    assert clock.monotonic() == 900.0
    assert wire.unpack(raw, clock=clock)["type"] == "chat"
    clock.advance_wall(0.001)
    with pytest.raises(ValueError, match="timestamp out of window"):
        wire.unpack(raw, clock=clock)


@pytest.mark.parametrize("offset", [-30.001, -30.0, 30.0, 30.001])
def test_replay_window_is_symmetric_and_includes_the_exact_boundary(offset):
    signer = _identity(bytes(range(32)))
    raw = wire.pack("chat", {"text": "boundary"}, signer=signer,
                    clock=FakeClock(wall_start=1000.0))
    receiver = FakeClock(start=123.0, wall_start=1000.0 + offset)
    if abs(offset) <= 30.0:
        assert wire.unpack(raw, clock=receiver)["payload"]["text"] == "boundary"
    else:
        with pytest.raises(ValueError, match="timestamp out of window"):
            wire.unpack(raw, clock=receiver)


@pytest.mark.parametrize("call", [
    lambda: wire.pack("chat", {}, signer=_identity(bytes(range(32)))),
    lambda: wire.unpack(b"{}"),
], ids=["pack", "unpack"])
def test_wire_requires_an_explicit_clock(call):
    with pytest.raises(TypeError, match="clock"):
        call()


def test_control_process_time_fires_the_injected_wall_clock_test(monkeypatch):
    """Control for test_wire_timestamp_and_replay_window_follow_only_injected_wall_time."""
    from holdem.p2p.timeout import RealClock

    original = wire.pack

    def process_time(action_type, payload, prev_hash="0" * 64, *, signer, clock):
        return original(action_type, payload, prev_hash,
                        signer=signer, clock=RealClock())

    monkeypatch.setattr(wire, "pack", process_time)
    with pytest.raises(AssertionError, match="bypassed the injected wall clock"):
        test_wire_timestamp_and_replay_window_follow_only_injected_wall_time()


@pytest.mark.parametrize("pubkey,signature", [
    (b"", b""),
    (bytes(31), bytes(64)),
    (bytes(32), bytes(63)),
    (bytes(32), bytes(64)),
])
def test_verification_rejects_bad_keys_and_signatures(pubkey, signature):
    assert verify(pubkey, b"message", signature) is False


def test_identity_import_creates_no_config_files(tmp_path):
    config = tmp_path / "config"
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, HOLDEM_CONFIG_DIR=str(config), PYTHONPATH=str(repo))
    probe = (
        "from holdem.p2p import identity\n"
        "assert identity._default_identity is None\n"
    )
    proc = subprocess.run([sys.executable, "-B", "-c", probe], cwd=tmp_path,
                          env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert not config.exists()
    assert list(tmp_path.iterdir()) == []


def test_default_identity_loads_once_and_legacy_helpers_use_it(tmp_path, monkeypatch):
    path = tmp_path / "identity.json"
    monkeypatch.setattr(identity, "_default_identity", None)
    monkeypatch.setattr(identity, "_identity_path", lambda: path)
    first = identity.load_or_create()
    assert path.exists()
    assert identity.load_or_create() is first
    assert identity.private_key() is first.private_key()
    assert identity.public_key_bytes() == first.public_key_bytes()
    assert identity.peer_id() == first.peer_id()
    assert identity.x25519_private_key() is first.x25519_private_key()
    assert identity.x25519_public_key_bytes() == first.x25519_public_key_bytes()
    assert identity.sign(b"message") == first.sign(b"message")
    assert identity.verify(first.public_key_bytes(), b"message", first.sign(b"message"))


def test_stored_identity_survives_a_reload_without_rewriting(tmp_path):
    path = tmp_path / "identity.json"
    first = identity.load_or_create(path)
    stored = path.read_bytes()
    second = identity.load_or_create(path)
    assert first is not second
    assert second.public_key_bytes() == first.public_key_bytes()
    assert second.x25519_public_key_bytes() == first.x25519_public_key_bytes()
    assert second.sign(b"after restart") == first.sign(b"after restart")
    assert path.read_bytes() == stored
    assert set(json.loads(stored)) == {"private_key_b64", "x25519_private_key_b64"}


def test_existing_ed25519_only_identity_keeps_its_signing_key(tmp_path):
    path = tmp_path / "identity.json"
    # The legacy file can predate the companion encryption key. Loading it
    # adds that key without replacing the signing identity or extra fields.
    path.write_text(json.dumps({
        "private_key_b64": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
        "nickname": "existing host",
    }))
    loaded = identity.load_or_create(path)
    assert loaded.public_key_bytes() == _identity(bytes(range(32))).public_key_bytes()
    stored = json.loads(path.read_text())
    assert stored["private_key_b64"] == "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
    assert stored["nickname"] == "existing host"
    assert "x25519_private_key_b64" in stored
