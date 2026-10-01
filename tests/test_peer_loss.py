"""Seated-peer loss: a table that still needs a seat ends when it drops.

Every hand's deal and audit need a share from every dealt seat, nothing
reconnects, and no turn timer is wired. So before this, a joiner whose
connection dropped mid-hand left the host waiting on it forever: the drop
popped the roster entry and the hand sat in betting with no way out.

Policy enforced here:

  in play   a seat the current or next hand needs -- every seat before the
            first hand, the dealt seats during a hand or after a void, the
            seats with chips after a settle -- ends the table with
            PEER_LOST and a reason naming the seat.
  not       a seat that has busted out, or any drop in the lobby, is just a
            roster change.

Chips stand at last_settled_stacks: an unsettled hand's pot is discarded,
not paid out.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.replica_table import PHASE_BETTING
from holdem.p2p.session import Player, Session

import importlib
try:
    importlib.import_module("holdem.p2p.elgamal")   # libsodium guard
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


def table(n=3, stacks=None, start=True):
    """n seated sessions on one bus, peer0 the host, game started."""
    bus = InMemoryBus()
    order = [f"peer{i}" for i in range(n)]
    sessions = {}
    for i, cid in enumerate(order):
        s = Session(is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
                    transport=InMemoryTransport(bus, cid),
                    master_secret=bytes([i + 1]) * 32)
        s.local_conn_id = cid
        s._host_conn_id = "peer0"
        s._join_order = list(order)
        for j, c in enumerate(order):
            s.players[c] = Player(conn_id=c, peer_id=c, nickname=f"P{j}",
                                  avatar_b64="")
        s.configure_seats(list(order))
        s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
        s.state = "PLAYING"
        bus.register(cid, s)
        sessions[cid] = s
    stacks = list(stacks or [500] * n)
    if start:
        for i, cid in enumerate(order):
            if stacks[i] > 0:                 # a busted seat is never dealt
                sessions[cid].start_p2p_hand(
                    hand_no=1, names=[f"P{j}" for j in range(n)],
                    stacks=stacks, sb=5, bb=10, button=0)
        bus.drain()
    return bus, sessions, order


def act(bus, sessions, order, action="call"):
    seat = sessions[order[0]].replica.actor
    assert sessions[order[seat]].send_bet_action(action) == "applied"
    bus.drain()


def settle_by_checkdown(bus, sessions, order):
    while sessions[order[0]].replica.phase == PHASE_BETTING:
        act(bus, sessions, order)
    assert all(sessions[c].hand_result is not None for c in order)


# --------------------------------------------------------------- in play

def test_seated_peer_lost_mid_hand_ends_the_table():
    bus, sessions, order = table(2)
    host = sessions["peer0"]
    act(bus, sessions, order)                    # chips into the pot
    assert host.replica.stacks != [500, 500]
    seen = []
    host.on_session_terminated = seen.append

    host.handle_disconnect("peer1")

    assert host.terminal_state == Session.PEER_LOST
    assert host.terminal_reason == "seat 1 (P1) disconnected"
    assert host.terminal_record.initiating_seat == 1
    assert host.terminal_record.conn_id == "peer1"
    assert len(seen) == 1
    # The in-flight pot is discarded: chips stand where the hand began.
    assert host.last_settled_stacks == [500, 500]


def test_peer_lost_in_a_later_hand_reverts_to_that_hands_settlement():
    bus, sessions, order = table(3)
    host = sessions["peer0"]
    settle_by_checkdown(bus, sessions, order)
    settled = host.replica.stacks
    assert host.last_settled_stacks == settled
    assert {sessions[c].next_p2p_hand() for c in order} == {"started"}
    bus.drain()
    act(bus, sessions, order)
    assert host.replica.stacks != settled        # hand 2's blinds and call

    host.handle_disconnect("peer2")

    assert host.terminal_state == Session.PEER_LOST
    assert host.terminal_reason == "seat 2 (P2) disconnected"
    assert host.last_settled_stacks == settled


def test_a_seat_with_chips_lost_between_hands_ends_the_table():
    """Settled, but the next hand cannot be dealt without that seat."""
    bus, sessions, order = table(3)
    settle_by_checkdown(bus, sessions, order)
    host = sessions["peer0"]
    host.handle_disconnect("peer1")
    assert host.terminal_state == Session.PEER_LOST
    assert host.last_settled_stacks == host.replica.stacks
    assert host.next_p2p_hand() == "session_over"


def test_a_seated_peer_lost_before_the_first_hand_ends_the_table():
    bus, sessions, order = table(3, start=False)
    host = sessions["peer0"]
    host.handle_disconnect("peer2")
    assert host.terminal_state == Session.PEER_LOST
    assert host.last_settled_stacks is None      # nothing was ever dealt


def test_a_voided_hands_seats_are_still_needed_for_the_redeal():
    bus, sessions, order = table(3)
    host = sessions["peer0"]
    for cid in order:
        sessions[cid]._void_hand("protocol failure")
    bus.drain()
    host.handle_disconnect("peer1")
    assert host.terminal_state == Session.PEER_LOST
    assert host.last_settled_stacks == [500, 500, 500]


# ----------------------------------------------------------- not in play

def test_a_seat_that_was_never_dealt_may_leave():
    """Seat 2 has no chips, so no hand deals it and nobody waits on it."""
    bus, sessions, order = table(3, stacks=[500, 500, 0])
    host = sessions["peer0"]
    assert host.replica.seats_dealt == [0, 1]
    host.handle_disconnect("peer2")
    assert host.terminal_state is None
    assert "peer2" not in host.players
    settle_by_checkdown(bus, {c: sessions[c] for c in order[:2]}, order[:2])
    host.handle_disconnect("peer2")              # still not needed
    assert host.terminal_state is None


def test_a_lobby_drop_is_a_roster_change():
    """Seats arranged but no game started and no hand dealt."""
    bus, sessions, order = table(3, start=False)
    host = sessions["peer0"]
    host.state = "LOBBY"
    assert "peer2" in host.seat_order
    host.handle_disconnect("peer2")
    assert host.terminal_state is None
    assert "peer2" not in host.players
