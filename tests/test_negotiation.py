"""Protocol version checking and capability freezing.

Two gaps this covers:

* wire envelopes carry a version field that was written as a literal and
  only ever checked for presence, so a peer speaking an incompatible
  version was accepted and its messages interpreted under this version's
  rules.

* game_start rewrote seat order and the table-wide deal policy with no
  check on the sender and no check on session state. Any peer could
  broadcast one mid-hand to reset the table or silently downgrade the
  deal -- a change no other peer would notice, since the policy is read
  from exactly this message.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p import wire
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.session import Player, Session

KEY       = Session.DEAL_POLICY_SETTING
BG        = Session.DEAL_POLICY_BG
DETECTION = Session.DEAL_POLICY_DETECTION


# --------------------------------------------------- protocol version

def test_pack_uses_the_declared_version():
    env = wire.safe_loads(wire.pack("chat", {"text": "hi"}))
    assert env["v"] == wire.PROTOCOL_VERSION


def test_unpack_accepts_the_current_version():
    assert wire.unpack(wire.pack("chat", {"text": "hi"}))["type"] == "chat"


@pytest.mark.parametrize("version", [0, 2, 99, -1])
def test_unpack_rejects_an_unsupported_version(version):
    """An incompatible peer must be refused before its payload is
    interpreted under rules it may not share."""
    raw = wire.pack("chat", {"text": "hi"})
    env = wire.safe_loads(raw)
    env["v"] = version
    with pytest.raises(ValueError, match="version"):
        wire.unpack(_reseal(env))


@pytest.mark.parametrize("version", ["1", 1.0, None, [1], {"v": 1}])
def test_unpack_rejects_a_non_integer_version(version):
    raw = wire.pack("chat", {"text": "hi"})
    env = wire.safe_loads(raw)
    env["v"] = version
    with pytest.raises(ValueError, match="version"):
        wire.unpack(_reseal(env))


def _reseal(env: dict) -> bytes:
    """Re-sign an edited envelope so the version check is what fails."""
    import hashlib
    import json

    from holdem.p2p import identity
    env = {k: v for k, v in env.items() if k not in ("sig", "hash")}
    canonical = json.dumps(env, sort_keys=True, separators=(",", ":")).encode()
    env["sig"] = identity.sign(canonical).hex()
    full = json.dumps(env, sort_keys=True, separators=(",", ":")).encode()
    env["hash"] = hashlib.sha256(full).hexdigest()
    return json.dumps(env).encode()


def test_supported_versions_includes_the_current_one():
    assert wire.PROTOCOL_VERSION in wire.SUPPORTED_VERSIONS


# ------------------------------------------------- capability freezing

def make_table(n=2, settings=None):
    bus = InMemoryBus()
    order = [f"peer{i}" for i in range(n)]
    sessions = {}
    for i, cid in enumerate(order):
        s = Session(is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
                    transport=InMemoryTransport(bus, cid),
                    master_secret=bytes([i + 1]) * 32)
        s.local_conn_id = cid
        if not s.is_host:
            s._host_conn_id = "peer0"
        bus.register(cid, s)
        sessions[cid] = s
    host = sessions["peer0"]
    for i, cid in enumerate(order):
        host.players[cid] = Player(conn_id=cid, peer_id=cid,
                                   nickname=f"P{i}", avatar_b64="")
    host.start_game(dict(settings if settings is not None else {KEY: BG}))
    bus.drain()
    return bus, sessions, order


def test_prevention_cannot_be_downgraded_mid_session():
    """The attack: the table agreed on prevention, then a peer broadcasts a
    second game_start with it turned off. Nothing else would notice --
    prevention is read from exactly this message."""
    bus, sessions, order = make_table(2, settings={KEY: BG})
    victim = sessions["peer1"]
    assert victim.prevention is True

    # From the REAL host. This previously came from "peer1" -- the victim
    # itself, whose _host_conn_id is "peer0" -- so the non-host guard
    # dropped it and the freeze branch never ran. The test passed while
    # duplicating test_game_start_from_a_non_host_is_refused.
    victim.handle_message("peer0", {
        "type": "game_start",
        "payload": {"seat_order": order, "table_settings": {KEY: DETECTION}},
    })
    assert victim.prevention is True, "prevention was silently downgraded"
    assert victim.deal_policy == BG


def test_seat_order_cannot_be_rewritten_mid_session():
    """Resetting seat order mid-session repoints every seat index, which
    would reassign hole cards and blame."""
    bus, sessions, order = make_table(3, settings={KEY: BG})
    victim = sessions["peer1"]
    before = list(victim._seat_order)

    victim.handle_message("peer2", {
        "type": "game_start",
        "payload": {"seat_order": ["peer2", "peer1", "peer0"],
                    "table_settings": {}},
    })
    assert victim._seat_order == before, "seat order was rewritten mid-session"


def test_game_start_from_a_non_host_is_refused():
    bus, sessions, order = make_table(2, settings={KEY: BG})
    victim = sessions["peer1"]
    victim.state = "LOBBY"          # even before play, only the host starts
    victim.handle_message("peer1", {
        "type": "game_start",
        "payload": {"seat_order": order, "table_settings": {KEY: DETECTION}},
    })
    assert victim.prevention is True
    assert victim.state == "LOBBY"


def test_host_game_start_still_works_from_the_lobby():
    """The freeze must not break the legitimate path."""
    bus = InMemoryBus()
    peer = Session(is_host=False, nickname="P1", avatar_b64="",
                   transport=InMemoryTransport(bus, "peer1"),
                   master_secret=b"\x02" * 32)
    peer.local_conn_id = "peer1"
    peer._host_conn_id = "peer0"
    bus.register("peer1", peer)

    peer.handle_message("peer0", {
        "type": "game_start",
        "payload": {"seat_order": ["peer0", "peer1"],
                    "table_settings": {KEY: BG}},
    })
    assert peer.state == "PLAYING"
    assert peer.prevention is True
    assert peer._seat_order == ["peer0", "peer1"]


def test_repeated_identical_game_start_is_harmless():
    """Duplicate delivery of the legitimate message must be idempotent,
    not a refusal that leaves the peer in a different state."""
    bus, sessions, order = make_table(2, settings={KEY: BG})
    peer = sessions["peer1"]
    peer.handle_message("peer0", {
        "type": "game_start",
        "payload": {"seat_order": order, "table_settings": {KEY: BG}},
    })
    assert peer.prevention is True
    assert peer._seat_order == order
    assert peer.state == "PLAYING"
