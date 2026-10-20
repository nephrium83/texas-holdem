"""Only verified timestamp faults are reported as clock disconnects."""
import asyncio
import json
import struct

import pytest

from holdem.p2p import identity, transport, wire
from holdem.p2p.timeout import FakeClock


def test_signed_clock_failure_is_typed_after_signature_and_hash_validation():
    sender = FakeClock(wall_start=1000)
    receiver = FakeClock(wall_start=1030)
    raw = wire.pack("heartbeat", {}, signer=identity.load_or_create(), clock=sender)
    assert wire.unpack(raw, clock=receiver)["ts"] == 1_000_000
    receiver.advance_wall(.001)
    with pytest.raises(wire.ClockWindowError) as caught:
        wire.unpack(raw, clock=receiver)
    assert caught.value.pubkey == identity.load_or_create().public_key_bytes().hex()
    assert caught.value.skew_ms == pytest.approx(30_001)

    bad_hash = json.loads(raw)
    bad_hash["hash"] = "0" * 64
    with pytest.raises(ValueError, match="Hash mismatch") as caught:
        wire.unpack(json.dumps(bad_hash).encode(), clock=receiver)
    assert not isinstance(caught.value, wire.ClockWindowError)

    bad_signature = json.loads(raw)
    bad_signature["ts"] = 0
    with pytest.raises(ValueError, match="Invalid signature") as caught:
        wire.unpack(json.dumps(bad_signature).encode(), clock=receiver)
    assert not isinstance(caught.value, wire.ClockWindowError)


class _Reader:
    def __init__(self, blob):
        self.blob = blob

    async def readexactly(self, count):
        result, self.blob = self.blob[:count], self.blob[count:]
        if len(result) != count:
            raise asyncio.IncompleteReadError(result, count)
        return result


class _Writer:
    closed = False

    def get_extra_info(self, _name):
        return None

    def close(self):
        self.closed = True


@pytest.fixture
def isolated_transport(monkeypatch):
    monkeypatch.setattr(transport, "_writers", {})
    monkeypatch.setattr(transport, "_disconnect_reasons", {})
    monkeypatch.setattr(transport, "_conn_callbacks", [])
    monkeypatch.setattr(transport, "_disc_callbacks", [])
    monkeypatch.setattr(transport, "_reason_disc_callbacks", [])
    monkeypatch.setattr(transport, "HEARTBEAT_INTERVAL", None)
    monkeypatch.setattr(transport, "SILENCE_TIMEOUT", None)
    monkeypatch.setattr(transport, "_deliver_event", lambda fn, *args: fn(*args))


def test_transport_clock_reason_preserves_legacy_callback_contract(
        isolated_transport, monkeypatch):
    clock = FakeClock(wall_start=1000)
    raw = wire.pack("heartbeat", {}, signer=identity.load_or_create(), clock=clock)
    clock.advance_wall(-31)
    monkeypatch.setattr(transport, "_clock", clock)
    legacy, reasons = [], []
    transport.on_disconnect(legacy.append)
    transport.on_disconnect(lambda cid, reason: reasons.append((cid, reason)),
                            with_reason=True)
    writer = _Writer()
    asyncio.run(transport._handle_connection(
        _Reader(struct.pack(">I", len(raw)) + raw), writer, "peer", "local"))
    assert writer.closed
    assert legacy == ["peer"]
    assert reasons == [("peer", "clock")]
    assert "peer" not in transport.peer_ids()


def test_explicit_disconnect_reason_is_forwarded_and_reset_clears_subscribers(
        isolated_transport, monkeypatch):
    class Loop:
        def call_soon_threadsafe(self, fn):
            fn()

    monkeypatch.setattr(transport, "_ensure_loop", lambda: None)
    monkeypatch.setattr(transport, "_loop", Loop())
    writer = _Writer()
    transport._writers["peer"] = writer
    reasons = []
    transport.on_disconnect(lambda cid, reason: reasons.append((cid, reason)),
                            with_reason=True)
    transport.disconnect("peer", reason="clock")
    assert writer.closed
    transport._run_disc_callbacks("peer")
    assert reasons == [("peer", "clock")]
    transport.reset_callbacks()
    transport._run_disc_callbacks("other", "heartbeat")
    assert reasons == [("peer", "clock")]
