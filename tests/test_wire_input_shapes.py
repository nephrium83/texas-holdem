"""Malformed peer representations fail at parsing, before callback mutation."""
import asyncio
import hashlib
import json
import struct
from copy import deepcopy
from types import SimpleNamespace

import pytest

from holdem.p2p import transport, wire
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.session import Player, Session
from holdem.p2p.timeout import FakeClock
from test_identity_injection import MemorySigner
from test_join_auth import _wire_up


CLOCK = FakeClock(wall_start=1234.5)
SIGNER = MemorySigner(23)


def _signed(changes):
    message = json.loads(wire.pack("chat", {"text": "valid"}, signer=SIGNER, clock=CLOCK))
    message.update(changes)
    unsigned = {k: v for k, v in message.items() if k not in ("sig", "hash")}
    message["sig"] = SIGNER.sign(json.dumps(
        unsigned, sort_keys=True, separators=(",", ":")).encode()).hex()
    if "sig" in changes:
        message["sig"] = changes["sig"]
    message["hash"] = hashlib.sha256(json.dumps(
        {k: v for k, v in message.items() if k != "hash"},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if "hash" in changes:
        message["hash"] = changes["hash"]
    return json.dumps(message).encode()


@pytest.mark.parametrize("field,value", [
    ("sig", None), ("sig", []), ("pubkey", {}), ("pubkey", 7),
    ("hash", None), ("hash", {}), ("ts", "1234500"), ("ts", True),
    ("ts", 1234500.25), ("ts", 10 ** 400), ("prev", []), ("prev", "short"),
    ("type", []), ("type", None), ("payload", None),
    ("payload", []), ("payload", "not-an-object"),
])
def test_malformed_envelopes_raise_only_peer_local_protocol_errors(field, value):
    with pytest.raises(ValueError) as error:
        wire.unpack(_signed({field: value}), clock=CLOCK)
    assert not isinstance(error.value, wire.ClockWindowError)


def test_valid_unknown_type_and_nested_payload_keep_their_exact_signed_content():
    raw = _signed({"type": "future-extension", "payload": {"shares": [[1, 2]], "optional": None}})
    assert wire.unpack(raw, clock=CLOCK) == json.loads(raw)


@pytest.mark.parametrize("message", [None, [], 3, {"type": []}, {"type": {}}])
def test_lan_parser_rejects_non_objects_and_unhashable_types(message):
    async def read():
        raw = json.dumps(message).encode()
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack(">I", len(raw)) + raw)
        reader.feed_eof()
        return await transport._read_msg(reader)

    with pytest.raises(ValueError):
        asyncio.run(read())


def _peer():
    session = Session(is_host=False, nickname="peer", avatar_b64="",
                      transport=InMemoryTransport(InMemoryBus(), "peer"),
                      clock=CLOCK, master_secret=bytes([3]) * 32)
    session.local_conn_id = "peer"
    session._host_conn_id = "host"
    session.players["peer"] = Player(conn_id="peer", peer_id="peer", nickname="Peer", avatar_b64="")
    return session


@pytest.mark.parametrize("message", [None, [], 3, {"type": []}, {"type": {}}])
def test_session_rejects_bad_dispatch_shapes_without_mutation(message):
    session = _peer()
    session.handle_message("host", message)
    assert session.state == "LOBBY" and session.local_conn_id == "peer"


@pytest.mark.parametrize("kind", ["player_ack", "player_list", "game_start", "ready", "chat", "game_state", "deal_private"])
@pytest.mark.parametrize("payload", [None, [], "not-an-object", 7, False])
def test_control_payloads_are_rejected_before_callbacks(kind, payload):
    session = _peer()
    callbacks = []
    session.on_chat = lambda *args: callbacks.append(args)
    session.on_game_state = lambda *args: callbacks.append(args)
    session.on_deal_private = lambda *args: callbacks.append(args)
    session.on_game_start = lambda *args: callbacks.append(args)
    session.handle_message("host", {"type": kind, "payload": payload})
    assert not callbacks and session.state == "LOBBY"
    assert session.local_conn_id == "peer"


@pytest.mark.parametrize("kind", ["player_ack", "player_list", "game_start", "ready", "chat", "game_state", "deal_private"])
@pytest.mark.parametrize("payload", [None, [], "not-an-object"])
def test_direct_control_handlers_reject_malformed_payloads(kind, payload):
    session = _peer()
    before = deepcopy(session._last_game_state)
    callbacks = []
    for callback in ("on_game_state", "on_deal_private", "on_chat", "on_game_start", "on_player_list_changed"):
        setattr(session, callback, lambda *args: callbacks.append(args))
    handler = getattr(session, "_on_" + kind)
    args = ({"payload": payload},)
    if kind not in ("game_state", "deal_private"):
        args = ("host", *args)
    handler(*args)
    assert session.state == "LOBBY" and session.local_conn_id == "peer"
    assert session._last_game_state == before and callbacks == []


@pytest.mark.parametrize("players", [None, "not-a-list", [None],
    [{"conn_id": "peer", "nickname": "changed"}, {"conn_id": []}],
    [{"conn_id": "peer", "nickname": "changed"}, {"conn_id": "other", "ready": []}],
])
def test_malformed_roster_is_rejected_before_any_partial_update(players):
    session = _peer()
    before = deepcopy(vars(session.players["peer"]))
    session._on_player_list("host", {"payload": {"players": players}})
    assert list(session.players) == ["peer"]
    assert vars(session.players["peer"]) == before


@pytest.mark.parametrize("changes", [{"action": []}, {"action": {}},
    {"seat": None}, {"seat": "0"}, {"seat": []}, {"seat": 0.5},
    {"seat": True}, {"amount": []}, {"amount": "10"}, {"amount": True}, {"amount": -1},
])
def test_direct_game_action_rejects_bad_shapes_before_applying(changes):
    session = _peer()
    session.is_host = True
    session._engine = SimpleNamespace(actor=0)
    session._seat_order = ["peer"]
    applied = []
    session.on_action = lambda *args: applied.append(args)
    payload = {"seat": 0, "action": "call", "amount": 0, **changes}
    session.handle_game_action("peer", {"payload": payload})
    assert applied == []


@pytest.mark.parametrize("field,value", [("pubkey", []), ("pubkey", {}),
    ("client_nonce", []), ("client_nonce", {}), ("server_nonce", 7),
])
def test_joiner_classifies_non_string_admission_fields_as_malformed(field, value):
    _, _, transport, _, session, auth, events = _wire_up()
    auth.begin("chosen-host")
    message = {"type": "admission_challenge", "pubkey": "01" * 32,
               "payload": {"client_nonce": "02" * 16, "server_nonce": "03" * 16}}
    if field == "pubkey":
        message[field] = value
    else:
        message["payload"][field] = value
    auth.route("chosen-host", message)
    assert len(events["failed"]) == 1
    assert "malformed" in events["failed"][0]
    assert transport.disconnected == ["chosen-host"]
    assert not session._host_authenticated and not auth.player_info_sent


def test_malformed_challenge_on_replaced_connection_cannot_fail_selected_host():
    _, _, transport, _, session, auth, events = _wire_up()
    auth.begin("chosen-host")
    auth.route("old-host", {"type": "admission_challenge", "payload": None, "pubkey": []})
    assert events == {"auth": [], "failed": []}
    assert transport.disconnected == []
    assert not session._host_authenticated
