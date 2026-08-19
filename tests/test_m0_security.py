"""M0 security cleanup — D1-D4 from issue #37.

Each test names the invariant, not the path that happened to be noticed.
The deliberate-break controls live in test_m0_security_controls.py.

D1  A partial seat->signing-key map must never be frozen as authoritative.
    _bind_seat_keys is one-way; freezing an incomplete map permanently
    strands every seat it could not resolve.

D2  player_info is a LOBBY message. Accepting it during PLAYING lets any
    holder of the room code mutate roster state mid-hand.

D3/D4 are probed here too, because the recorded severity did not survive
    contact with the code and the negative result is the finding.
"""
import sys
from pathlib import Path

from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.session import (
    AUTHOR_MODE_COMPAT, AUTHOR_MODE_WIRE, Player, Session,
)


def wire_session(cid="peer0", is_host=False):
    """A session in wire mode, where seat bindings are REQUIRED."""
    bus = InMemoryBus()
    s = Session(is_host=is_host, nickname="P", avatar_b64="",
                transport=InMemoryTransport(bus, cid),
                author_mode=AUTHOR_MODE_WIRE,
                master_secret=b"\x02" * 32)
    s.local_conn_id = cid
    bus.register(cid, s)
    return s


def seated(s, keys):
    """Populate seat order and roster. ``keys`` maps conn_id -> pubkey hex
    ('' means the roster has no verified key for that seat)."""
    s._seat_order = list(keys)
    for cid, key in keys.items():
        s.players[cid] = Player(conn_id=cid, peer_id=cid, nickname=cid,
                                avatar_b64="", ed25519_pubkey_hex=key)
    return s


# ---------------------------------------------------------------- D1

def test_d1_partial_seat_key_map_is_never_frozen():
    """The invariant: _seat_keys is authoritative, so it is all or nothing.

    Discriminating observable: after the freeze, either every seat in
    _seat_order resolves to a key, or none does. A map covering SOME seats
    is the poison state -- it is one-way, so the unresolved seats can never
    be authorized for the rest of the session.
    """
    s = seated(wire_session(), {"a": "AA", "b": "BB", "c": ""})

    with pytest.raises(RuntimeError, match="incomplete"):
        s._bind_seat_keys()

    assert s._seat_keys == {}, (
        "a refused binding must leave the map unfrozen, so a later complete "
        "attempt can still succeed")


def test_d1_complete_map_still_freezes():
    s = seated(wire_session(), {"a": "AA", "b": "BB", "c": "CC"})
    s._bind_seat_keys()
    assert s._seat_keys == {0: "AA", 1: "BB", 2: "CC"}


def test_d1_empty_map_is_legitimate_and_unchanged():
    """Compat transports carry no envelopes, so no seat has a verified key.
    That is the documented fallback, not a partial map, and must not raise.
    """
    bus = InMemoryBus()
    s = Session(is_host=False, nickname="P", avatar_b64="",
                transport=InMemoryTransport(bus, "peer0"),
                master_secret=b"\x02" * 32)
    s.local_conn_id = "peer0"
    seated(s, {"a": "", "b": "", "c": ""})
    assert s.author_mode == AUTHOR_MODE_COMPAT

    s._bind_seat_keys()

    assert s._seat_keys == {}
    assert s._author_owns_seat("a", None, 0) is True, (
        "compat must still fall through to the conn_id rule")


def test_d1_disconnect_before_freeze_cannot_strand_a_seat():
    """The reachable form: a peer drops between start_game and the freeze,
    so handle_disconnect has already popped it from players."""
    s = seated(wire_session(), {"a": "AA", "b": "BB", "c": "CC"})
    s.players.pop("c")                      # dropped in the freeze window

    with pytest.raises(RuntimeError, match="incomplete"):
        s._bind_seat_keys()

    assert s._seat_keys == {}


# ---------------------------------------------------------------- D2

def compat_host(cid="host"):
    """A host on a compat transport. D2 is a lifecycle defect, not a wire
    one, and a wire host additionally requires an admission policy."""
    bus = InMemoryBus()
    s = Session(is_host=True, nickname="H", avatar_b64="",
                transport=InMemoryTransport(bus, cid),
                master_secret=b"" * 32)
    s.local_conn_id = cid
    s._host_conn_id = cid
    bus.register(cid, s)
    return s


def _host_in_play():
    s = compat_host()
    seated(s, {"host": "HH", "p1": "11"})
    s.state = "PLAYING"
    return s


def test_d2_player_info_is_refused_outside_lobby():
    """The invariant: roster identity is established in LOBBY. Once the
    table is PLAYING, no inbound message may add a seat-less participant
    or reorder join state.
    """
    s = _host_in_play()
    before_players = set(s.players)
    before_join = list(s._join_order)

    s._on_player_info("intruder", {"pubkey": "ZZ",
                                   "payload": {"nickname": "mallory"}})

    assert set(s.players) == before_players, (
        "a PLAYING table accepted a new roster entry")
    assert list(s._join_order) == before_join


def test_d2_player_info_still_works_in_lobby():
    s = compat_host()
    s.state = "LOBBY"
    s._on_player_info("joiner", {"pubkey": "JJ",
                                 "payload": {"nickname": "legit"}})
    assert "joiner" in s.players
    assert s.players["joiner"].nickname == "legit"
    assert "joiner" in s._join_order


def test_d2_terminated_session_refuses_player_info():
    s = compat_host()
    s.state = "LOBBY"
    s.terminate("TEST", "done")
    s._on_player_info("joiner", {"pubkey": "JJ", "payload": {}})
    assert "joiner" not in s.players


# ---------------------------------------------------------------- D4

def test_d4_driver_prevention_cannot_be_omitted():
    """The invariant: a peer never runs the deal without stating its
    prevention mode. The insecure state must not be reachable by omission.

    This is the residual real risk behind D4. The shipped compat path
    (sidecar) hard-codes Bayer-Groth and the policy is table-wide through a
    single writer, so a BG table does enforce BG -- but MentalDealDriver
    defaulted prevention to False, meaning any future construction site
    that forgot the argument would silently deal detection-only.
    """
    from holdem.p2p.mental_deal_driver import MentalDealDriver

    with pytest.raises(TypeError):
        MentalDealDriver(session_id="s", hand_no=1, local_seat=0,
                         seats_in=[0, 1], button=0, master_secret=b"m",
                         send=lambda m: None)


def test_d4_bg_policy_forces_prevention_regardless_of_author_mode():
    """Enforcement is keyed to the ADOPTED POLICY, not to author_mode.

    A compat table that settled on Bayer-Groth must still refuse to deal
    without prevention. Previously the mandate read
    ``author_mode == WIRE and not prevention``, so the same disagreement on
    a compat table sailed through.
    """
    bus = InMemoryBus()
    s = Session(is_host=False, nickname="P", avatar_b64="",
                transport=InMemoryTransport(bus, "peer0"),
                master_secret=b"\x02" * 32)
    assert s.author_mode == AUTHOR_MODE_COMPAT
    s._adopt_deal_policy(Session.DEAL_POLICY_BG)

    assert s.prevention is True
    s._assert_deal_preconditions()          # must not raise on an honest table

    # The disagreement the guard exists to catch: the policy says
    # Bayer-Groth, but this peer would deal without prevention. Today
    # prevention is DERIVED from the policy, so the two cannot drift on
    # their own -- forcing them apart is how we prove the guard is
    # load-bearing rather than decorative.
    with mock.patch.object(type(s), "prevention",
                           property(lambda self: False)):
        assert s.prevention is False
        with pytest.raises(RuntimeError,
                           match="every participating peer path"):
            s._assert_deal_preconditions()


def test_d4_detection_only_compat_table_remains_legal():
    """Do not force deliberately detection-only harnesses onto BG."""
    bus = InMemoryBus()
    s = Session(is_host=False, nickname="P", avatar_b64="",
                transport=InMemoryTransport(bus, "peer0"),
                master_secret=b"\x02" * 32)
    s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
    assert s.prevention is False
    s._assert_deal_preconditions()          # legal in compat, must not raise
