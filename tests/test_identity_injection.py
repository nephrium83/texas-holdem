"""Two peers in one process own different keys without a machine identity.

This is a signed admission exchange, not a transport double that merely
labels dictionaries as verified: every delivered envelope passes through
wire.pack/unpack, and the production Session and JoinAuthenticator drive
both halves of admission. No sockets, GUI, persisted identity or deal are
needed to establish the two seats.
"""
from __future__ import annotations

import json
from collections import deque
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from holdem.p2p import admission, invite, wire
from holdem.p2p.join_auth import JoinAuthenticator, joiner_admission_from_invite
from holdem.p2p.session import Session
from holdem.p2p.timeout import FakeClock
from test_core_boundary import _fresh_import


class MemorySigner:
    """An independent Ed25519 key implementing only the core's signer API."""

    def __init__(self, seed: int):
        self._key = Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)

    def public_key_bytes(self) -> bytes:
        return self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def sign(self, message: bytes) -> bytes:
        return self._key.sign(message)


class SignedEndpoint:
    """Queued transport; production code chooses messages and recipients."""

    delivers_verified_envelopes = True

    def __init__(self, name, signer, queue, clock=None):
        self.name = name
        self.signer = signer
        self.clock = clock if clock is not None else FakeClock()
        self.queue = queue
        self.peers = {}
        self.receive = None
        self.sent = []
        self.disconnected = []

    def send(self, conn_id, message):
        if "sig" in message:
            # JoinAuthenticator has already signed player_info. Re-signing
            # it here would conceal whether it used its injected signer.
            raw = json.dumps(message).encode()
        else:
            payload = dict(message.get("payload") or {})
            payload.update({k: v for k, v in message.items()
                            if k not in ("type", "payload")})
            raw = wire.pack(message["type"], payload,
                            signer=self.signer, clock=self.clock)
        self.sent.append(wire.unpack(raw, clock=self.clock))
        self.queue.append((self.peers[conn_id], self.name, raw))

    def broadcast(self, message):
        for conn_id in self.peers:
            self.send(conn_id, message)

    def broadcast_except(self, exclude, message):
        for conn_id in self.peers:
            if conn_id != exclude:
                self.send(conn_id, message)

    def disconnect(self, conn_id):
        self.disconnected.append(conn_id)


def _signed_pair():
    host_signer, joiner_signer = MemorySigner(1), MemorySigner(2)
    host_key = host_signer.public_key_bytes()
    joiner_key = joiner_signer.public_key_bytes()
    parsed = invite.parse_room_code(invite.generate_room_code(
        host_pubkey=host_key))
    policy = admission.HostAdmission(
        admission_secret=bytes.fromhex(parsed["admission_secret"]),
        host_pubkey=host_key,
        discovery_token=bytes.fromhex(parsed["discovery_token"]))
    pin = joiner_admission_from_invite(parsed, joiner_pubkey=joiner_key)
    queue = deque()
    clock = FakeClock(wall_start=1234.5)
    host_wire = SignedEndpoint("host", host_signer, queue, clock)
    joiner_wire = SignedEndpoint("joiner", joiner_signer, queue, clock)
    host_wire.peers["joiner"] = joiner_wire
    joiner_wire.peers["host"] = host_wire
    host = Session(is_host=True, nickname="Host", avatar_b64="",
                   transport=host_wire, admission=policy, clock=clock,
                   master_secret=bytes([9]) * 32)
    joiner = Session(is_host=False, nickname="Joiner", avatar_b64="",
                     transport=joiner_wire, joiner_admission=pin, clock=clock,
                     master_secret=bytes([8]) * 32)
    events = {"authenticated": [], "failed": []}
    auth = JoinAuthenticator(
        transport=joiner_wire, session=joiner, joiner_admission=pin,
        signer=joiner_signer, nickname="Joiner",
        on_authenticated=events["authenticated"].append,
        on_failed=events["failed"].append, clock=clock)
    host_wire.receive = host.handle_message
    joiner_wire.receive = auth.route
    host.local_conn_id = "host"
    host.add_local_player("host", public_key=host_key,
                          x25519_public_key=bytes([3]) * 32)
    auth.begin("host")
    delivered = 0
    while queue:
        target, sender, raw = queue.popleft()
        target.receive(sender, wire.unpack(raw, clock=target.clock))
        delivered += 1
        assert delivered < 32, "signed admission did not reach quiescence"
    return host, joiner, policy, auth, events, host_wire, joiner_wire


def test_wire_uses_only_the_requested_signer():
    """Interleaving independent keys must not change either author's identity."""
    first, second = MemorySigner(1), MemorySigner(2)
    clock = FakeClock()
    for signer in (first, second, first):
        raw = wire.pack("chat", {"text": "hello"}, signer=signer, clock=clock)
        message = wire.unpack(raw, clock=clock)
        assert message["pubkey"] == signer.public_key_bytes().hex(), (
            "wire.pack used another peer's signing key")
        # Independent of the implementation's verify helper: a verifier
        # that simply returned True could not make this signature pass.
        unsigned = {k: v for k, v in message.items() if k not in ("sig", "hash")}
        canonical = json.dumps(unsigned, sort_keys=True,
                               separators=(",", ":")).encode()
        signer._key.public_key().verify(bytes.fromhex(message["sig"]), canonical)


def test_two_signed_peers_keep_independent_identity_and_write_no_config(
        tmp_path, monkeypatch):
    config = tmp_path / "no-machine-identity"
    monkeypatch.setenv("HOLDEM_CONFIG_DIR", str(config))
    host, joiner, policy, auth, events, host_wire, joiner_wire = _signed_pair()
    host_key = host_wire.signer.public_key_bytes().hex()
    joiner_key = joiner_wire.signer.public_key_bytes().hex()
    assert host_key != joiner_key
    assert events == {"authenticated": ["host"], "failed": []}
    assert policy.admitted_key("joiner") == bytes.fromhex(joiner_key)
    assert auth.player_info_sent
    assert host.players["host"].ed25519_pubkey_hex == host_key, (
        "host seat lost the injected signing key")
    assert set(host.players) == {"host", "joiner"}, (
        "the independently signed joiner was not seated")
    assert host.players["joiner"].ed25519_pubkey_hex == joiner_key
    assert host.players["host"].x25519_pubkey_hex == (bytes([3]) * 32).hex()
    assert {cid: p.ed25519_pubkey_hex for cid, p in joiner.players.items()} == {
        "host": host_key, "joiner": joiner_key}
    assert joiner.local_conn_id == "joiner"
    assert {m["pubkey"] for m in host_wire.sent} == {host_key}
    assert {m["pubkey"] for m in joiner_wire.sent} == {joiner_key}
    assert {m["ts"] for m in host_wire.sent + joiner_wire.sent} == {1234500}
    assert not config.exists(), "core admission created machine identity files"


def test_importing_host_identity_does_not_create_key_files(tmp_path):
    """The host loads keys explicitly; merely importing its adapter is inert."""
    report = _fresh_import("holdem.p2p.identity", tmp_path)
    assert report["failed"] is None, report["failed"]
    assert not report["writes"], "importing identity wrote to disk"
    assert not report["files"], "importing identity created config files"
    assert report["threads"] == 0


@pytest.mark.parametrize("assigned", ["joiner", "host", "another-connection"])
def test_admitted_guest_ack_cannot_rename_host_or_gain_self_delivery(assigned):
    host, joiner, policy, auth, events, host_wire, joiner_wire = _signed_pair()
    assert events == {"authenticated": ["host"], "failed": []}
    assert policy.is_admitted("joiner")
    host.configure_seats(["host", "joiner"])
    host._bind_seat_keys()

    raw = wire.pack("player_ack", {"your_conn_id": assigned},
                    signer=joiner_wire.signer, clock=joiner_wire.clock)
    host.handle_message("joiner", wire.unpack(raw, clock=host_wire.clock))

    assert host.local_conn_id == "host", "a guest renamed the host"
    forged = wire.unpack(wire.pack(
        "bet_action", {"seat": 0, "hand": host._hand_no,
                       "author_seq": 0, "action": "fold"},
        signer=joiner_wire.signer, clock=joiner_wire.clock),
        clock=host_wire.clock)
    incoming = host._normalize_hostless("joiner", forged)
    assert not incoming.local
    assert not host._admit_hostless(incoming), "guest acquired host-seat authority"
    assert host._author_seq_seen == {}, "refused claims consumed a sequence"
    local = host._normalize_hostless("host", {
        "type": "bet_action", "seat": 0, "hand": host._hand_no})
    assert local.local and host._admit_hostless(local)
    assert joiner.local_conn_id == "joiner"


@pytest.mark.parametrize("payload", [None, [], "not-a-payload", {}])
def test_host_refuses_ack_before_parsing_payload(payload):
    host, _, _, _, _, _, joiner_wire = _signed_pair()
    raw = wire.pack("player_ack", payload,
                    signer=joiner_wire.signer, clock=joiner_wire.clock)
    host.handle_message("joiner", wire.unpack(raw, clock=joiner_wire.clock))
    assert host.local_conn_id == "host"


def test_authenticated_joiner_still_adopts_host_acknowledgement():
    host, joiner, _, _, _, host_wire, _ = _signed_pair()
    joiner.local_conn_id = "pending-assignment"
    raw = wire.pack("player_ack", {"your_conn_id": "assigned-by-host"},
                    signer=host_wire.signer, clock=host_wire.clock)
    joiner.handle_message("host", wire.unpack(raw, clock=host_wire.clock))
    assert joiner.local_conn_id == "assigned-by-host"
    assert joiner._host_conn_id == "host"
    assert host.local_conn_id == "host"


def test_tk_action_sender_supplies_the_host_identity(monkeypatch):
    from holdem.p2p import identity, transport

    signer = MemorySigner(4)
    monkeypatch.setattr(identity, "load_or_create", lambda: signer)
    from holdem.gui import Holdem

    sent = []
    monkeypatch.setattr(transport, "broadcast", sent.append)
    ui = SimpleNamespace(_mp_local_seat=2)
    Holdem._mp_send_action(ui, "raise", 150)
    assert len(sent) == 1
    message = wire.unpack(json.dumps(sent[0]).encode(), clock=transport.get_clock())
    assert message["pubkey"] == signer.public_key_bytes().hex()
    assert message["type"] == "action"
    assert message["payload"] == {"seat": 2, "action": "raise", "amount": 150}


@pytest.mark.parametrize("is_host", [False, True], ids=["peer", "host"])
def test_tk_chat_sender_supplies_identity_and_preserves_echo(monkeypatch, is_host):
    from holdem.p2p import identity, transport

    signer = MemorySigner(5)
    monkeypatch.setattr(identity, "load_or_create", lambda: signer)
    from holdem.gui import Holdem

    sent, echoed = [], []
    monkeypatch.setattr(transport, "broadcast", sent.append)
    ui = SimpleNamespace(nickname="Ada", _mp_is_host=is_host,
                         _append_chat=lambda *entry: echoed.append(entry))
    Holdem._mp_send_chat(ui, "Good hand")
    assert len(sent) == 1
    message = wire.unpack(json.dumps(sent[0]).encode(), clock=transport.get_clock())
    assert message["pubkey"] == signer.public_key_bytes().hex()
    assert message["type"] == "chat"
    assert message["payload"] == {"text": "Good hand", "nickname": "Ada"}
    assert echoed == ([("Ada", "Good hand")] if is_host else [])


def test_join_authenticator_rejects_a_signer_outside_its_admission_transcript():
    host, joiner, other = MemorySigner(1), MemorySigner(2), MemorySigner(3)
    policy = admission.JoinerAdmission(
        admission_secret=bytes(range(16)), host_pubkey=host.public_key_bytes(),
        joiner_pubkey=joiner.public_key_bytes(), discovery_token=bytes(range(8)))
    transport = SignedEndpoint("joiner", joiner, deque())
    with pytest.raises(ValueError, match="signer.*admission key"):
        JoinAuthenticator(transport=transport, session=object(),
                          joiner_admission=policy, signer=other, clock=FakeClock())
    assert transport.sent == []


@pytest.mark.parametrize("call, missing", [
    (lambda: wire.pack("chat", {"text": "no signer"}, clock=FakeClock()), "signer"),
    (lambda: invite.generate_room_code(), "host_pubkey"),
    (lambda: joiner_admission_from_invite({}), "joiner_pubkey"),
    (lambda: JoinAuthenticator(transport=object(), session=object(),
                               joiner_admission=object(), clock=FakeClock()), "signer"),
    (lambda: Session(is_host=False, nickname="Local", avatar_b64="",
                     transport=object(), author_mode="compat", clock=FakeClock(),
                     master_secret=bytes([9]) * 32).add_local_player(
                         "local"), "public_key"),
], ids=["wire", "invite", "admission", "authenticator", "local-seat"])
def test_explicit_identity_dependencies_are_required(call, missing):
    """Omitting an identity must fail rather than choose a machine default."""
    with pytest.raises(TypeError, match=missing):
        call()


def test_control_another_signer_fires_the_wire_identity_test(monkeypatch):
    """Control for test_wire_uses_only_the_requested_signer."""
    original = wire.pack
    another_peer = MemorySigner(3)

    def wrong_signer(action_type, payload, prev_hash="0" * 64, *, signer, clock):
        return original(action_type, payload, prev_hash,
                        signer=another_peer, clock=clock)

    monkeypatch.setattr(wire, "pack", wrong_signer)
    with pytest.raises(AssertionError, match="another peer's signing key"):
        test_wire_uses_only_the_requested_signer()


def test_control_a_swapped_host_key_fires_the_two_peer_test(tmp_path, monkeypatch):
    """Control for the two-peer invariant: a host must use its injected key."""
    original = Session.add_local_player

    def swapped_key(self, conn_id, *, public_key, x25519_public_key=b""):
        return original(self, conn_id,
                        public_key=MemorySigner(3).public_key_bytes(),
                        x25519_public_key=x25519_public_key)

    monkeypatch.setattr(Session, "add_local_player", swapped_key)
    with pytest.raises(AssertionError, match="host seat lost the injected signing key"):
        test_two_signed_peers_keep_independent_identity_and_write_no_config(
            tmp_path, monkeypatch)
