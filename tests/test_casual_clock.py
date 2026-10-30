"""Pregame clock agreement uses actual signed three-player star traffic."""
from collections import deque
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from holdem.p2p import admission, wire
from holdem.p2p.casual_clock import PROFILE, lobby_clocks
from holdem.p2p.session import Player, Session
from holdem.p2p.timeout import FakeClock
from tests.admission_helpers import admit


class Signer:
    def __init__(self, number):
        self.key = Ed25519PrivateKey.from_private_bytes(bytes([number + 1]) * 32)

    def public_key_bytes(self):
        return self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def sign(self, message):
        return self.key.sign(message)


class Endpoint:
    delivers_verified_envelopes = True

    def __init__(self, table, cid, signer, clock):
        self.table, self.cid, self.signer, self.clock = table, cid, signer, clock
        self.sent = []

    def send(self, destination, message):
        if "sig" in message:
            raw = json.dumps(message).encode()
        else:
            payload = dict(message.get("payload", {}))
            payload.update({key: value for key, value in message.items()
                            if key not in ("type", "payload")})
            raw = wire.pack(message["type"], payload,
                            signer=self.signer, clock=self.clock)
        self.sent.append(json.loads(raw))
        self.table.pending.append((destination, self.cid, raw))

    def broadcast(self, message):
        for destination in self.table.order if self.cid == "P0" else ["P0"]:
            if destination != self.cid:
                self.send(destination, message)

    def broadcast_except(self, exclude, message):
        assert self.cid == "P0"
        for destination in self.table.order:
            if destination not in (self.cid, exclude):
                self.send(destination, message)

    def disconnect(self, _cid, **_kwargs):
        pass


class SignedTable:
    """Real signatures and production authorization; only sockets are replaced."""
    def __init__(self, offsets=(0, 0, 0)):
        self.order = ["P0", "P1", "P2"]
        self.pending = deque()
        self.sessions = {}
        self.signers = [Signer(seat) for seat in range(3)]
        self.clocks = [FakeClock(wall_start=1000 + offset) for offset in offsets]
        policy = admission.HostAdmission(admission_secret=b"a" * 16,
                                         host_pubkey=self.signers[0].public_key_bytes())
        for seat, cid in enumerate(self.order):
            endpoint = Endpoint(self, cid, self.signers[seat], self.clocks[seat])
            s = Session(is_host=seat == 0, nickname=cid, avatar_b64="",
                        transport=endpoint, clock=self.clocks[seat],
                        master_secret=bytes([seat + 1]) * 32,
                        admission=policy if seat == 0 else None)
            s.local_conn_id, s._host_conn_id = cid, "P0"
            s._pinned_host_pubkey = self.signers[0].public_key_bytes().hex()
            for other, name in enumerate(self.order):
                s.players[name] = Player(
                    conn_id=name, peer_id=name, nickname=name, avatar_b64="",
                    ed25519_pubkey_hex=self.signers[other].public_key_bytes().hex(),
                    is_host=other == 0, ready=True)
            self.sessions[cid] = s
        for seat in [1, 2]:
            admit(policy, self.order[seat], self.signers[seat].public_key_bytes(),
                  b"a" * 16, self.signers[0].public_key_bytes())

    def drain(self, suppress=None):
        while self.pending:
            destination, source, raw = self.pending.popleft()
            envelope = wire.unpack(raw, clock=self.sessions[destination]._clock)
            if suppress is not None and suppress(destination, source, envelope):
                continue
            self.sessions[destination].handle_message(source, envelope)

    def round(self):
        self.sessions["P0"].reliability_tick()
        self.drain()

    def advance(self, seconds):
        for clock in self.clocks:
            clock.advance(seconds)


@pytest.mark.parametrize("offsets", [(0, 0, 0), (0, -10, 10), (0, 20, 0)])
def test_every_replica_checks_all_pairs_at_the_twenty_second_boundary(offsets):
    table = SignedTable(offsets)
    table.round()
    assert all(s.reliability_lobby_status()["ready"] for s in table.sessions.values())
    # Clock traffic from P1 reaches P2 unchanged through P0, not re-signed.
    relayed = [message for message in table.sessions["P0"]._transport.sent
               if message["type"] == "casual_clock_sample"]
    assert {message["pubkey"] for message in relayed} == {
        table.signers[1].public_key_bytes().hex(),
        table.signers[2].public_key_bytes().hex()}


def test_joiners_twenty_one_seconds_apart_cannot_start_even_if_each_near_host():
    table = SignedTable((0, -10.5, 10.5))
    table.round()
    for s in table.sessions.values():
        status = s.reliability_lobby_status()
        assert not status["ready"]
        assert "20 seconds" in status["error"]
        assert "P1" in status["error"] or "P2" in status["error"]
        assert s.state == "LOBBY" and s.replica is None


def test_missing_or_wrong_capability_sample_blocks_start():
    table = SignedTable()
    table.sessions["P0"].reliability_tick()
    table.drain(suppress=lambda _to, _frm, message:
                message["type"] == "casual_clock_sample"
                and message["pubkey"] == table.signers[2].public_key_bytes().hex())
    host = table.sessions["P0"]
    assert not host.reliability_lobby_status()["ready"]
    clocks = lobby_clocks(host)
    payload = dict(round=clocks.round, roster=clocks.roster,
                   wall=1000, profile=PROFILE + ".unknown")
    host._transport.table.pending.append((
        "P0", "P2", wire.pack("casual_clock_sample", payload,
                              signer=table.signers[2], clock=table.clocks[2])))
    table.drain()
    assert not host.reliability_lobby_status()["ready"]


def test_unsigned_identity_substitution_cannot_satisfy_missing_clock_sample():
    table = SignedTable()
    table.sessions["P0"].reliability_tick()
    table.drain(suppress=lambda _to, _frm, message:
                message["type"] == "casual_clock_sample"
                and message["pubkey"] == table.signers[2].public_key_bytes().hex())
    host = table.sessions["P0"]
    clocks = lobby_clocks(host)
    raw = wire.pack("casual_clock_sample", dict(
        round=clocks.round, roster=clocks.roster, wall=1000, profile=PROFILE),
        signer=table.signers[1], clock=table.clocks[1])
    forged = json.loads(raw)
    forged["pubkey"] = table.signers[2].public_key_bytes().hex()
    with pytest.raises(ValueError, match="Invalid signature"):
        wire.unpack(json.dumps(forged).encode(), clock=table.clocks[0])
    assert not host.reliability_lobby_status()["ready"]


def test_replaying_a_sample_does_not_refresh_the_finished_round():
    table = SignedTable()
    table.round()
    host = table.sessions["P0"]
    sample = next(message for message in table.sessions["P1"]._transport.sent
                  if message["type"] == "casual_clock_sample")
    table.advance(15.001)
    # Still inside the wire timestamp window, but no longer a fresh table check.
    host.handle_message("P1", wire.unpack(json.dumps(sample).encode(), clock=table.clocks[0]))
    assert not host.reliability_lobby_status()["ready"]


def test_roster_change_requires_a_new_complete_round():
    table = SignedTable()
    table.round()
    host = table.sessions["P0"]
    old = lobby_clocks(host).round
    host.players["P2"].ed25519_pubkey_hex = Signer(5).public_key_bytes().hex()
    assert not host.reliability_lobby_status()["ready"]
    host.reliability_tick()
    assert lobby_clocks(host).round != old
    assert not host.reliability_lobby_status()["ready"]


def test_replaying_an_older_request_cannot_replace_a_newer_round():
    table = SignedTable()
    table.round()
    request = next(message for message in table.sessions["P0"]._transport.sent
                   if message["type"] == "casual_clock_request")
    table.advance(10)
    table.round()
    joiner = table.sessions["P1"]
    current = lobby_clocks(joiner).round
    assert current != request["payload"]["round"]
    joiner.handle_message("P0", wire.unpack(json.dumps(request).encode(), clock=table.clocks[1]))
    assert lobby_clocks(joiner).round == current
