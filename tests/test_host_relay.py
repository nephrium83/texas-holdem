"""Authenticated host relay over a STAR topology.

tests/test_three_peer_topology.py proved with three production processes
that only the host listens, joiners only dial it, and a joiner's
broadcast reaches the host and nobody else -- so a three-seat hostless
hand cannot leave KEYGEN.

InMemoryBus cannot exercise the fix, because it is a mesh: a broadcast
reaches everyone regardless of who is connected to whom. These tests use
a star bus instead, so "B's message only reaches A unless A forwards it"
is a property of the harness rather than something the test has to
remember not to violate.

The host is a COURIER. It forwards the envelope it received -- same v,
type, payload, pubkey, ts, prev, sig, hash -- and never re-signs,
substitutes its pubkey, rebuilds the payload, or rewrites the claimed
seat. Re-serialization is fine: wire.unpack verifies over canonical field
values, not original bytes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p.events import EventSink
    from holdem.p2p.session import Player, Session
except RuntimeError as exc:                          # pragma: no cover
    pytest.skip(f"libsodium unavailable: {exc}", allow_module_level=True)


KEY = {"A": "aa" * 32, "B": "bb" * 32, "C": "cc" * 32}
KEY_X = "ee" * 32                                    # a stranger


class _Sink(EventSink):
    def emit(self, event):
        pass


class StarBus:
    """A ↔ B and A ↔ C. No B ↔ C. Exactly the production graph."""

    def __init__(self):
        self.sessions: dict = {}
        self.delivered: list = []       # (to, from, msg)

    def link(self, cid, session):
        self.sessions[cid] = session

    def _peers_of(self, cid):
        # Only the host has more than one edge.
        return [c for c in self.sessions if c != cid] if cid == "A" else ["A"]

    def deliver(self, sender, target, msg):
        self.delivered.append((target, sender, msg))
        self.sessions[target].handle_message(sender, dict(msg))


class StarTransport:
    def __init__(self, bus: StarBus, cid: str):
        self._bus, self._cid = bus, cid

    def attach(self, s):
        pass

    def set_on_disconnect(self, cb):
        pass

    def broadcast(self, msg):
        for t in self._bus._peers_of(self._cid):
            self._bus.deliver(self._cid, t, msg)

    def broadcast_except(self, exclude, msg):
        for t in self._bus._peers_of(self._cid):
            if t != exclude:
                self._bus.deliver(self._cid, t, msg)

    def send(self, to, msg):
        if to in self._bus.sessions:
            self._bus.deliver(self._cid, to, msg)


def _table():
    bus = StarBus()
    made = {}
    for cid in ("A", "B", "C"):
        s = Session(is_host=(cid == "A"), nickname=cid, avatar_b64="",
                    transport=StarTransport(bus, cid), sink=_Sink())
        s.local_conn_id = cid
        s._host_conn_id = "A"
        s._seat_order = ["A", "B", "C"]
        for other in ("A", "B", "C"):
            s.players[other] = Player(conn_id=other, peer_id="",
                                      nickname=other, avatar_b64="",
                                      ed25519_pubkey_hex=KEY[other])
        s._bind_seat_keys()
        s._hand_no = 1
        made[cid] = s
        bus.link(cid, s)
    return bus, made


def _env(author: str, seat: int, mtype="key_announce", **extra):
    payload = {"seat": seat, "hand": 1}
    payload.update(extra)
    return {"v": 1, "type": mtype, "pubkey": KEY[author] if author in KEY
            else author, "ts": 111, "prev": "0" * 64, "sig": "ff" * 64,
            "hash": "ab" * 32, "payload": payload}


def _received_by(bus, who, mtype="key_announce"):
    return [m for (to, _frm, m) in bus.delivered
            if to == who and m.get("type") == mtype]


# --------------------------------------------------------- the core property

def test_host_relays_a_joiner_message_to_the_other_joiner():
    """B -> A -> C. Without relay, C never sees it (proved in
    tests/test_three_peer_topology.py against real processes)."""
    bus, s = _table()
    env = _env("B", seat=1)
    s["A"].handle_message("B", dict(env))
    assert _received_by(bus, "C"), "the host did not relay B's message to C"


def test_relay_preserves_every_signed_field():
    """Courier, not author. The eight signed fields must be identical."""
    bus, s = _table()
    env = _env("B", seat=1)
    s["A"].handle_message("B", dict(env))
    got = _received_by(bus, "C")[0]
    for field in ("v", "type", "payload", "pubkey", "ts", "prev", "sig",
                  "hash"):
        assert got[field] == env[field], f"host altered {field!r} in transit"


def test_relayed_message_authenticates_as_the_original_author():
    """C sees B's key and B's seat, though the hop was C<->A."""
    bus, s = _table()
    env = _env("B", seat=1)
    s["A"].handle_message("B", dict(env))
    got = _received_by(bus, "C")[0]
    assert got["pubkey"] == KEY["B"]
    assert got["payload"]["seat"] == 1
    body = s["C"]._hostless_body(got)
    assert s["C"]._seat_author_ok("A", body, 1) is True, (
        "C refused a B-authored message because the host delivered it")


def test_host_does_not_echo_the_relay_back_to_its_author():
    bus, s = _table()
    s["A"].handle_message("B", _env("B", seat=1))
    assert not _received_by(bus, "B"), "the host echoed B's own message back"


# ------------------------------------------------------------- host misuse

def test_recipient_rejects_a_host_resigned_copy():
    """A host that re-signs becomes the author, and loses the seat."""
    bus, s = _table()
    forged = _env("B", seat=1)
    forged["pubkey"] = KEY["A"]                      # host re-signed
    body = s["C"]._hostless_body(forged)
    assert s["C"]._seat_author_ok("A", body, 1) is False


def test_recipient_rejects_a_host_modified_payload():
    """Altering the payload while keeping B's signature must not pass.

    The signature check lives in wire.unpack, which never sees this
    tampered object -- so the property asserted here is the one the
    session layer owns: the SEAT the payload claims is still authorized
    against the author, so a host that rewrites the seat cannot promote
    itself, and one that rewrites other payload fields is caught by
    unpack on the real path.
    """
    bus, s = _table()
    tampered = _env("B", seat=1)
    tampered["payload"] = dict(tampered["payload"], seat=0)   # claim host seat
    body = s["C"]._hostless_body(tampered)
    assert s["C"]._seat_author_ok("A", body, 0) is False, (
        "a host rewrote the claimed seat and kept authority")


def test_host_refuses_to_relay_a_stranger_key():
    """Validly signed by a key that owns no seat -- dropped at the host,
    not fanned out to the table."""
    bus, s = _table()
    s["A"].handle_message("B", _env(KEY_X, seat=1))
    assert not _received_by(bus, "C"), (
        "the host amplified a message its recipients would reject")


def test_host_refuses_to_relay_a_seat_speaking_for_another():
    bus, s = _table()
    s["A"].handle_message("B", _env("B", seat=2))    # B claiming C's seat
    assert not _received_by(bus, "C")


def test_only_the_host_relays():
    """A joiner receiving a relay must not fan it out again."""
    bus, s = _table()
    s["C"].handle_message("A", _env("B", seat=1))
    assert not [m for (to, frm, m) in bus.delivered if frm == "C"], (
        "a joiner re-relayed a message it received")


# ------------------------------------------------------- every hostless type

@pytest.mark.parametrize("mtype", [
    "key_announce", "deck_round", "deal_share", "audit_open",
    "bet_action", "hand_void", "session_end", "timeout_proposal",
])
def test_every_hostless_type_is_relayed(mtype):
    """All eight, not the six originally noticed.

    A type missing from _HOSTLESS_PAYLOAD_TYPES is not merely unrelayed --
    it also skips the envelope unwrap, so its author never reaches the
    seat check. Lifecycle messages (session_end, timeout_proposal) are the
    easy ones to forget, and forgetting them strands a three-seat table at
    exactly the moments it needs to agree: elimination and timeout.
    """
    bus, s = _table()
    seat_field = "seat_from" if mtype == "deal_share" else "seat"
    env = _env("B", seat=1, mtype=mtype)
    env["payload"] = {seat_field: 1, "hand": 1}
    s["A"].handle_message("B", dict(env))
    assert _received_by(bus, "C", mtype), f"{mtype} was not relayed to C"
    assert not _received_by(bus, "B", mtype), f"{mtype} was echoed to its author"
