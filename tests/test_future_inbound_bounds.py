"""Early authenticated traffic keeps its identity within finite storage."""
import json

import pytest

from holdem.p2p import wire
from test_author_sequence_bounds import _host


def _message(remote, sequence=0, hand=2, **extra):
    return wire.unpack(wire.pack("key_announce", {
        "seat": 1, "hand": hand, "author_seq": sequence, **extra},
        signer=remote.signer, clock=remote.clock), clock=remote.clock)


def _body(entry):
    return entry.incoming.envelope if hasattr(entry, "incoming") else entry[1]


def test_far_future_hand_is_not_stored_or_relayed(monkeypatch):
    host, remote = _host()
    relayed = []
    monkeypatch.setattr(host, "_relay_if_host", relayed.append)
    host.handle_message("joiner", _message(remote, hand=1000000))
    assert host._msg_buffer == [] and relayed == []


def test_identical_early_envelopes_take_one_slot_and_one_relay(monkeypatch):
    host, remote = _host()
    relayed = []
    monkeypatch.setattr(host, "_relay_if_host", relayed.append)
    message = _message(remote)
    for _ in range(20):
        host.handle_message("joiner", message)
    assert len(host._msg_buffer) == 1 and len(relayed) == 1


def test_future_storage_has_an_author_quota():
    host, remote = _host()
    for sequence in range(80):
        host.handle_message("joiner", _message(remote, sequence))
    assert 0 < len(host._msg_buffer) <= 16
    assert host._author_seq_seen == {}


def test_future_storage_is_bounded_by_bytes_before_an_author_fills_its_slots():
    host, remote = _host()
    for sequence in range(16):
        host.handle_message("joiner", _message(remote, sequence, padding="x" * 300000))
    size = sum(len(json.dumps(_body(entry)).encode()) for entry in host._msg_buffer)
    assert 0 < size <= 2 * 1024 * 1024


def test_early_wire_message_replay_keeps_signed_identity_and_does_not_relay_twice(monkeypatch):
    host, remote = _host()
    applied, relayed = [], []
    def handler(conn_id, body):
        if host._hand_msg_ok(conn_id, body):
            applied.append(body)
    monkeypatch.setattr(host, "_on_deal_message", handler)
    monkeypatch.setattr(host, "_relay_if_host", relayed.append)
    message = _message(remote)
    host.handle_message("joiner", message)
    assert applied == [] and len(host._msg_buffer) == 1
    host._hand_no = 2
    with host._owner:
        host._replay_buffer()
    assert len(applied) == 1 and len(relayed) == 1
    assert host._author_seq_seen[(2, 1)][0] == message["hash"]
    host.handle_message("joiner", message)
    assert len(applied) == 1 and not host.hand_voided


def test_caller_mutation_cannot_change_a_queued_envelope(monkeypatch):
    host, remote = _host()
    message = _message(remote, nested={"value": "original"})
    host.handle_message("joiner", message)
    message["payload"]["nested"]["value"] = "changed"
    queued = _body(host._msg_buffer[0])
    body = queued.get("payload", queued)
    assert body["nested"]["value"] == "original"


def test_arbitrary_initial_hand_id_uses_one_startup_bucket_and_stale_buckets_are_purged():
    host, remote = _host()
    host._hand_no = 0
    host.handle_message("joiner", _message(remote, hand=10000))
    host.handle_message("joiner", _message(remote, sequence=1, hand=10001))
    assert len(host._msg_buffer) == 1 and host._msg_buffer[0].incoming.hand == 10000
    host._hand_no = 1
    with host._owner:
        host._replay_buffer()
    assert host._msg_buffer == []


@pytest.mark.parametrize("kind,handler", [
    ("key_announce", "_on_deal_message"), ("deck_round", "_on_deal_message"),
    ("deal_share", "_on_deal_message"), ("audit_open", "_on_deal_message"),
    ("bet_action", "_on_bet_action"), ("hand_void", "_on_hand_void"),
    ("hand_settled", "_on_hand_settled"),
])
def test_shared_hand_dispatch_preserves_each_current_handler(monkeypatch, kind, handler):
    host, remote = _host()
    called = []
    monkeypatch.setattr(host, handler, lambda cid, body: called.append((cid, body["type"])))
    message = wire.unpack(wire.pack(kind, {"seat": 1, "hand": 1, "author_seq": 0},
                                    signer=remote.signer, clock=remote.clock), clock=remote.clock)
    host.handle_message("joiner", message)
    assert called == [("joiner", kind)]


def test_retry_between_hand_advance_and_replay_is_not_relayed_or_applied_twice(monkeypatch):
    host, remote = _host()
    applied, relayed = [], []
    def handler(cid, body):
        if host._hand_msg_ok(cid, body):
            applied.append(body)
    monkeypatch.setattr(host, "_on_deal_message", handler)
    monkeypatch.setattr(host, "_relay_if_host", relayed.append)
    message = _message(remote)
    host.handle_message("joiner", message)
    host._hand_no = 2
    host.handle_message("joiner", message)
    with host._owner:
        host._replay_buffer()
    assert len(applied) == len(relayed) == 1 and host._msg_buffer == []
    assert not host.hand_voided


def test_revoked_connection_cannot_apply_its_queued_message(monkeypatch):
    host, remote = _host()
    applied = []
    host.handle_message("joiner", _message(remote))
    host._admission.forget("joiner")
    host._hand_no = 2
    monkeypatch.setattr(host, "_on_deal_message", lambda *args: applied.append(args))
    with host._owner:
        host._replay_buffer()
    assert applied == [] and host._author_seq_seen == {} and host._msg_buffer == []


def test_terminal_transition_during_replay_cannot_restore_pending_messages(monkeypatch):
    host, remote = _host()
    host.handle_message("joiner", _message(remote, sequence=0))
    host.handle_message("joiner", _message(remote, sequence=1))
    host._hand_no = 2
    calls = []
    def stop(*args):
        calls.append(args)
        host.terminate(host.ABORTED_PROTOCOL, "controlled terminal transition")
    monkeypatch.setattr(host, "_on_deal_message", stop)
    with host._owner:
        host._replay_buffer()
    assert len(calls) == 1 and host._msg_buffer == []


def test_reentrant_retry_of_later_pending_message_keeps_one_relay(monkeypatch):
    host, remote = _host()
    first, second = _message(remote, sequence=0), _message(remote, sequence=1)
    relayed, applied = [], []
    monkeypatch.setattr(host, "_relay_if_host", lambda ctx: relayed.append(ctx.body["author_seq"]))
    host.handle_message("joiner", first)
    host.handle_message("joiner", second)
    def handle(cid, body):
        applied.append(body["author_seq"])
        if body["author_seq"] == 0:
            host.handle_message("joiner", second)
    monkeypatch.setattr(host, "_on_deal_message", handle)
    host._hand_no = 2
    with host._owner:
        host._replay_buffer()
    assert relayed == [0, 1] and applied == [0, 1]
    assert host._active_replays == [] and host._msg_buffer == []
