"""Pins table-wide prevention mode at the session layer.

Prevention is a property of the TABLE, not of a peer. It rides in the
game_start table_settings every peer already receives, so peers reach the
same mode without negotiating and without a new message type. This file
pins that propagation, the backwards-compatible default, and the local
policy that stops a host from silently downgrading a table.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
    from holdem.p2p.session import Player, Session
except RuntimeError as exc:
    pytest.skip(f"libsodium unavailable: {exc}", allow_module_level=True)


KEY = Session.PREVENTION_SETTING


def make_table(n=2, settings=None, require=frozenset()):
    """n started Sessions on one bus, following tests/test_session_inmemory.

    ``require`` holds the seat indices whose local policy demands
    prevention -- a per-peer setting, deliberately distinct from the
    table-wide mode in ``settings``.
    """
    bus = InMemoryBus()
    sessions = {}
    for i in range(n):
        cid = f"peer{i}"
        s = Session(is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
                    transport=InMemoryTransport(bus, cid),
                    require_prevention=(i in require))
        s.local_conn_id = cid
        if not s.is_host:
            s._host_conn_id = "peer0"
        bus.register(cid, s)
        sessions[cid] = s
    host = sessions["peer0"]
    for i in range(n):
        host.players[f"peer{i}"] = Player(conn_id=f"peer{i}",
                                          peer_id=f"peer{i}",
                                          nickname=f"P{i}", avatar_b64="")
    host.start_game(dict(settings or {}))
    bus.drain()
    return sessions, list(sessions)


# ------------------------------------------------------------ defaults

def test_prevention_defaults_off():
    """Detection-only stays the v1 default."""
    sessions, _ = make_table(2, settings={})
    for s in sessions.values():
        assert s.prevention is False


def test_absent_key_reads_as_detection_only():
    """A table created by an older build has no such key at all."""
    sessions, _ = make_table(2, settings={"small_blind": 1, "big_blind": 2})
    for s in sessions.values():
        assert s.prevention is False


# --------------------------------------------------------- propagation

def test_prevention_propagates_to_every_peer():
    sessions, _ = make_table(3, settings={KEY: True})
    for conn_id, s in sessions.items():
        assert s.prevention is True, f"{conn_id} missed the table mode"


def test_host_and_peers_agree_without_negotiating():
    """No peer announces a mode; they all read the same broadcast."""
    on = make_table(4, settings={KEY: True})[0]
    off = make_table(4, settings={KEY: False})[0]
    assert len({s.prevention for s in on.values()}) == 1
    assert len({s.prevention for s in off.values()}) == 1
    assert next(iter(on.values())).prevention is True
    assert next(iter(off.values())).prevention is False


def test_non_boolean_setting_is_coerced_not_trusted():
    """A truthy non-bool must not leak a non-bool into the coordinator."""
    sessions, _ = make_table(2, settings={KEY: "yes"})
    for s in sessions.values():
        assert s.prevention is True
        assert isinstance(s.prevention, bool)


def test_mode_does_not_survive_into_a_later_table():
    """_last_table_settings is only overwritten when non-empty, so the mode
    is tracked separately; a second game_start with no settings must read
    as detection-only rather than inheriting the first table's True."""
    sessions, _ = make_table(2, settings={KEY: True})
    peer = sessions["peer1"]
    assert peer.prevention is True
    peer._on_game_start({"payload": {"seat_order": ["peer0", "peer1"],
                                     "table_settings": {}}})
    assert peer.prevention is False


# ------------------------------------------------------- driver wiring

def test_driver_receives_the_table_mode():
    for enabled in (False, True):
        sessions, order = make_table(2, settings={KEY: enabled})
        for s in sessions.values():
            s.begin_hand(hand_no=1, button=0)
            assert s._deal_driver.deal.prevention is enabled


def test_detection_only_hand_emits_no_proof():
    """The observable consequence of the default: nothing on the wire."""
    sessions, _ = make_table(2, settings={})
    for s in sessions.values():
        s.begin_hand(hand_no=1, button=0)
    for s in sessions.values():
        for msg in s._deal_outbox:
            assert "proof" not in msg


# ------------------------------------------------ anti-downgrade policy

def test_peer_requiring_prevention_refuses_a_downgraded_table():
    """A host that omits the setting would otherwise turn prevention off
    for everyone silently. A peer with a local policy fails closed."""
    sessions, _ = make_table(2, settings={}, require={1})
    with pytest.raises(RuntimeError, match="requires Bayer-Groth"):
        sessions["peer1"].begin_hand(hand_no=1, button=0)


def test_peer_requiring_prevention_accepts_a_prevention_table():
    sessions, _ = make_table(2, settings={KEY: True}, require={1})
    sessions["peer1"].begin_hand(hand_no=1, button=0)
    assert sessions["peer1"]._deal_driver.deal.prevention is True


def test_requirement_is_local_policy_not_table_state():
    """Requiring prevention must not itself turn prevention on."""
    sessions, _ = make_table(2, settings={}, require={0, 1})
    for s in sessions.values():
        assert s.prevention is False


def test_default_peer_does_not_require_prevention():
    sessions, _ = make_table(2, settings={})
    for s in sessions.values():
        assert s.require_prevention is False
        s.begin_hand(hand_no=1, button=0)      # must not raise
