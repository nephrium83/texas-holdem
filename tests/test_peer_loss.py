"""Seated-peer loss: a table that still needs a seat ends when it drops.

Every hand's deal and audit need a share from every dealt seat, nothing
reconnects, and no turn timer is wired. So before this, a joiner whose
connection dropped mid-hand left the host waiting on it forever: the drop
popped the roster entry and the hand sat in betting with no way out.

Policy enforced here:

  in play   a seat the current or next hand needs -- every seat before the
            first hand, the dealt seats during a hand or after a void, the
            seats with chips after a settle and any dealt seat whose
            settlement report the next hand still waits on -- ends the
            table with PEER_LOST and a reason naming the seat.
  not       a seat that has busted out, any seat once a settle leaves at
            most one seat with chips (the match is decided), or any drop in
            the lobby, is just a roster change.

Chips stand at last_settled_stacks: an unsettled hand's pot is discarded,
not paid out.

A busted host cannot judge a drop -- its replica stopped at the hand it
busted in -- so it only reports one, and the seats still playing decide.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.replica_table import PHASE_BETTING
from holdem.p2p.session import Player, Session
from tests.showdown_rig import rig_showdowns

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


def shove(r, seat):
    lg = r.engine.legal(seat)
    return ("raise", lg["max_to"]) if lg["can_raise"] else ("call", 0)


def folds(*seats):
    """Shove, except that the given seats fold."""
    return lambda r, seat: ("fold", 0) if seat in seats else shove(r, seat)


def play(bus, sessions, order, choose, ref=0):
    """Play the hand out, choose(replica, seat) -> (action, amount), read
    from seat ref's replica (one still dealt in)."""
    while sessions[order[ref]].replica.phase == PHASE_BETTING:
        r = sessions[order[ref]].replica
        seat = r.actor
        verdict = sessions[order[seat]].send_bet_action(*choose(r, seat))
        assert verdict == "applied"
        bus.drain()


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


def test_a_seat_that_busts_may_leave_before_the_next_hand(monkeypatch):
    """Seat 2 settles with nothing and leaves before Next. The next hand
    deals seats 0 and 1 only, so nobody waits on it and play goes on."""
    rig_showdowns(monkeypatch)(1, 2, 0)
    bus, sessions, order = table(3, stacks=[500, 500, 20])
    play(bus, sessions, order, folds(0))
    host = sessions["peer0"]
    # Seat 1 opens all in, seat 2 calls all in for 20 and seat 0 folds its
    # big blind: seat 1's aces take 20 + 20 + 10.
    assert host.replica.stacks == [490, 530, 0]
    bus.unregister("peer2")

    host.handle_disconnect("peer2")

    assert host.terminal_state is None
    assert {sessions[c].next_p2p_hand() for c in order[:2]} == {"started"}
    bus.drain()
    for c in order[:2]:
        assert sessions[c].terminal_state is None
        assert sessions[c].replica.seats_dealt == [0, 1]
        assert None not in sessions[c].deal_hole_cards


def test_a_seat_that_busts_and_leaves_before_reporting_ends_the_table(
        monkeypatch):
    """As above, except seat 2 goes before its settlement report does. The
    next hand waits on that report, which can no longer come, so the seat
    is still needed: the table ends rather than waiting for it forever.
    The hand settled everywhere, so it stands."""
    rig_showdowns(monkeypatch)(1, 2, 0)
    bus, sessions, order = table(3, stacks=[500, 500, 20])
    leaver = sessions["peer2"]
    send = leaver._transport.broadcast
    leaver._transport.broadcast = lambda msg: (
        None if msg.get("type") == "hand_settled" else send(msg))
    play(bus, sessions, order, folds(0))
    host = sessions["peer0"]
    assert host.replica.stacks == [490, 530, 0]
    assert host.next_p2p_hand() == "not_ready"
    bus.unregister("peer2")

    host.handle_disconnect("peer2")
    bus.drain()

    for c in order[:2]:
        assert sessions[c].terminal_state == Session.PEER_LOST, c
        assert sessions[c].last_settled_stacks == [490, 530, 0]


@pytest.mark.parametrize("leaver", ["winner", "loser"])
def test_a_decided_match_ends_normally_whoever_leaves_first(monkeypatch,
                                                            leaver):
    """A heads-up all-in busts one seat, and the joiner closes its client
    before the host presses Next. Nothing is left to deal, so its leaving
    costs the table nothing: the match ends normally, not as a lost peer.
    """
    rig_showdowns(monkeypatch)(*((1, 0) if leaver == "winner" else (0, 1)))
    bus, sessions, order = table(2, stacks=[500, 500])
    play(bus, sessions, order, shove)
    host = sessions["peer0"]
    final = [0, 1000] if leaver == "winner" else [1000, 0]
    assert host.replica.stacks == final
    bus.unregister("peer1")

    host.handle_disconnect("peer1")

    assert host.terminal_state is None
    assert host.next_p2p_hand() == "session_over"
    assert host.terminal_state == Session.ENDED_NORMAL
    assert host.last_settled_stacks == final


def test_a_lobby_drop_is_a_roster_change():
    """Seats arranged but no game started and no hand dealt."""
    bus, sessions, order = table(3, start=False)
    host = sessions["peer0"]
    host.state = "LOBBY"
    assert "peer2" in host.seat_order
    host.handle_disconnect("peer2")
    assert host.terminal_state is None
    assert "peer2" not in host.players


# ------------------------------------------------- every survivor learns it

def test_every_survivor_ends_when_one_seat_drops():
    """Only the host's socket closes when a joiner drops (the production
    graph is a star). The host's signed peer_lost notice is how the other
    joiner finds out; without it that joiner waits forever."""
    bus, sessions, order = table(3)
    host, other = sessions["peer0"], sessions["peer1"]
    act(bus, sessions, order)
    bus.unregister("peer2")                      # the process is gone

    host.handle_disconnect("peer2")
    bus.drain()

    assert other.terminal_state == Session.PEER_LOST
    assert other.terminal_reason == (
        "seat 2 (P2) disconnected (reported by seat 0)")
    assert other.terminal_record.initiating_seat == 2
    assert other.last_settled_stacks == host.last_settled_stacks \
        == [500, 500, 500]


def test_a_survivor_a_hand_ahead_still_ends():
    """Hands begin when each peer calls next_p2p_hand, so the reporter can
    still be on the last hand while a survivor has dealt the next one. The
    notice is not hand-scoped: the table is over either way."""
    bus, sessions, order = table(3)
    settle_by_checkdown(bus, sessions, order)
    settled = sessions["peer0"].replica.stacks
    ahead = sessions["peer1"]
    assert ahead.next_p2p_hand() == "started"
    bus.drain()
    assert ahead._hand_no == 2 and sessions["peer0"]._hand_no == 1
    bus.unregister("peer2")

    sessions["peer0"].handle_disconnect("peer2")
    bus.drain()

    assert ahead.terminal_state == Session.PEER_LOST
    assert ahead.last_settled_stacks == settled


def test_a_busted_spectator_learns_the_table_ended():
    bus, sessions, order = table(3)
    spectator = sessions["peer1"]
    spectator._p2p_spectator = True
    bus.unregister("peer2")
    sessions["peer0"].handle_disconnect("peer2")
    bus.drain()
    assert spectator.terminal_state == Session.PEER_LOST


@pytest.mark.parametrize("reporter,lost,stacks", [
    (1, 2, [500, 500, 500]),
    (2, 1, [500, 500, 0]),
], ids=["a-joiner-blaming-a-connected-seat", "a-seat-never-dealt"])
def test_a_notice_from_any_seat_but_the_host_is_dropped(reporter, lost,
                                                       stacks):
    """Only the host sees a joiner's socket close. A notice signed by any
    other seat passes ingress -- it is that seat's own -- but claims what
    its sender could not have observed, so it ends nobody's table. Not
    even shaped as a confirmation: the host reported no seat lost."""
    bus, sessions, order = table(3, stacks=stacks)
    for cid in order:
        if cid != order[reporter]:
            sessions[cid].handle_message(order[reporter], {
                "type": "peer_lost", "hand": 1, "seat": reporter,
                "lost_seat": lost, "ended": True})
    assert [sessions[c].terminal_state for c in order] == [None] * 3


# ------------------------------------------------- a busted host reports

def busted_host_table(monkeypatch):
    """Four seats. The host busts in hand 1 and seat 3 in hand 2, while
    seats 1 and 2 play on. The host's replica stays at hand 1, where seat
    3 still had chips: the stale view it must not judge a drop from."""
    rank = rig_showdowns(monkeypatch)
    rank(1, 0, 2, 3)
    bus, sessions, order = table(4, stacks=[20, 500, 500, 100])
    # Hand 1: the host shoves 20, seat 1 covers it and the blinds fold;
    # seat 1's aces take 20 + 20 + 5 + 10.
    play(bus, sessions, order, folds(2, 3))
    assert sessions["peer1"].replica.stacks == [0, 535, 495, 90]
    assert [sessions[c].next_p2p_hand() for c in order] == [
        "eliminated", "started", "started", "started"]
    bus.drain()
    # Hand 2: seat 2 folds, seat 3 shoves its 90 from the small blind and
    # seat 1 covers it from the big; aces again take 90 + 90.
    rank(1, 3, 2, 0)
    play(bus, sessions, order, folds(2), ref=1)
    assert sessions["peer1"].replica.stacks == [0, 625, 495, 0]
    assert sessions["peer0"].replica.stacks == [0, 535, 495, 90]
    return bus, sessions, order


def test_a_busted_host_lets_a_seat_that_busted_since_leave(monkeypatch):
    """Seat 3 leaves before Next. The host's stale replica says it holds
    90 chips; the seats still playing know it holds none. The host only
    reports the drop, they see no need for the seat, and play goes on."""
    bus, sessions, order = busted_host_table(monkeypatch)
    bus.unregister("peer3")

    sessions["peer0"].handle_disconnect("peer3")
    bus.drain()

    assert [sessions[c].terminal_state for c in order[:3]] == [None] * 3
    assert {sessions[c].next_p2p_hand() for c in order[1:3]} == {"started"}
    bus.drain()
    for c in order[1:3]:
        assert sessions[c].replica.seats_dealt == [1, 2]
        assert None not in sessions[c].deal_hole_cards
    assert sessions["peer0"].terminal_state is None      # still the relay


def test_a_confirmation_counts_only_for_the_seat_the_host_reported(
        monkeypatch):
    """The host has reported seat 3, which nobody needed, and play goes on.
    Seat 1 then sends a confirmation naming seat 2, which is connected and
    which the host never reported. A report of one seat must not let a
    joiner end the table on another and pin the blame on it."""
    bus, sessions, order = busted_host_table(monkeypatch)
    bus.unregister("peer3")
    sessions["peer0"].handle_disconnect("peer3")
    bus.drain()
    assert [sessions[c].terminal_state for c in order[:3]] == [None] * 3

    sessions["peer1"]._send_hostless({"type": "peer_lost",
                                      "hand": sessions["peer1"]._hand_no,
                                      "lost_seat": 2, "ended": True})
    bus.drain()

    assert [sessions[c].terminal_state for c in order[:3]] == [None] * 3
    assert {sessions[c].next_p2p_hand() for c in order[1:3]} == {"started"}


def test_a_busted_host_reports_a_needed_seat_and_every_survivor_ends(
        monkeypatch):
    """Seat 3 has busted too and stays to watch. Seat 2 drops mid-hand.
    The host cannot judge it, so it only reports; seat 1 still needs seat
    2, ends the table and confirms. The confirmation is how the busted
    host and seat 3, neither of which can judge, learn the table is over.
    """
    bus, sessions, order = busted_host_table(monkeypatch)
    assert [sessions[c].next_p2p_hand() for c in order[1:]] == [
        "started", "started", "eliminated"]
    bus.drain()
    actor = sessions["peer1"].replica.actor
    assert sessions[order[actor]].send_bet_action("call") == "applied"
    bus.drain()
    bus.unregister("peer2")

    sessions["peer0"].handle_disconnect("peer2")
    bus.drain()

    for c in ("peer0", "peer1", "peer3"):
        assert sessions[c].terminal_state == Session.PEER_LOST, c
        assert sessions[c].terminal_record.initiating_seat == 2
    assert sessions["peer1"].terminal_reason == (
        "seat 2 (P2) disconnected (reported by seat 0)")
    # The host dropped seat 2 from its roster when the socket closed, so
    # by the time the confirmation names it, it has no nickname there.
    assert sessions["peer0"].terminal_reason == (
        "seat 2 disconnected (reported by seat 1)")
    assert sessions["peer3"].terminal_reason == (
        "seat 2 (P2) disconnected (reported by seat 1)")
    assert sessions["peer1"].last_settled_stacks == [0, 625, 495, 0]


def test_a_busted_host_lets_the_winner_end_the_match_normally(monkeypatch):
    """Three seats. The host busts in hand 1, seat 2 in hand 2, and seat 2
    leaves before the winner presses Next. The host's stale replica still
    gives seat 2 chips; the winner knows the match is decided."""
    rank = rig_showdowns(monkeypatch)
    rank(1, 0, 2)
    bus, sessions, order = table(3, stacks=[20, 500, 500])
    # Seat 1 shoves, seat 2 folds its small blind and the host calls all
    # in for 20 from the big blind: seat 1's aces take 20 + 20 + 5.
    play(bus, sessions, order, folds(2))
    assert sessions["peer1"].replica.stacks == [0, 525, 495]
    assert [sessions[c].next_p2p_hand() for c in order] == [
        "eliminated", "started", "started"]
    bus.drain()
    rank(1, 2, 0)
    play(bus, sessions, order, shove, ref=1)
    assert sessions["peer1"].replica.stacks == [0, 1020, 0]
    bus.unregister("peer2")

    sessions["peer0"].handle_disconnect("peer2")
    bus.drain()

    assert [sessions[c].terminal_state for c in order[:2]] == [None, None]
    assert sessions["peer1"].next_p2p_hand() == "session_over"
    bus.drain()
    for c in order[:2]:
        assert sessions[c].terminal_state == Session.ENDED_NORMAL, c
        assert sessions[c].last_settled_stacks == [0, 1020, 0]


def test_a_busted_joiner_does_not_judge_the_drop_either(monkeypatch):
    """The host and seat 2 both bust in hand 1, seat 3 in hand 2, and seat
    3 leaves before the winner presses Next. Seat 2's replica stopped at
    hand 1, where seat 3 held 490: were it to judge the host's report it
    would confirm the drop and end a match that is already decided."""
    rank = rig_showdowns(monkeypatch)
    rank(1, 0, 2, 3)
    bus, sessions, order = table(4, stacks=[20, 500, 20, 500])
    # The host shoves 20, seat 1 shoves over it, seat 2 calls all in for
    # 20 from the small blind and seat 3 folds its big blind: seat 1's
    # aces take 20 + 20 + 20 + 10.
    play(bus, sessions, order, folds(3))
    assert sessions["peer1"].replica.stacks == [0, 550, 0, 490]
    assert [sessions[c].next_p2p_hand() for c in order] == [
        "eliminated", "started", "eliminated", "started"]
    bus.drain()
    rank(1, 3, 0, 2)
    play(bus, sessions, order, shove, ref=1)
    assert sessions["peer1"].replica.stacks == [0, 1040, 0, 0]
    bus.unregister("peer3")

    sessions["peer0"].handle_disconnect("peer3")
    bus.drain()

    assert [sessions[c].terminal_state for c in order[:3]] == [None] * 3
    assert sessions["peer1"].next_p2p_hand() == "session_over"
    bus.drain()
    for c in order[:3]:
        assert sessions[c].terminal_state == Session.ENDED_NORMAL, c
        assert sessions[c].last_settled_stacks == [0, 1040, 0, 0]


@pytest.mark.parametrize("msg", [
    # peer2 does not hold seat 0, so it cannot report as seat 0
    {"type": "peer_lost", "hand": 1, "seat": 0, "lost_seat": 1},
    # unattributable: no reporting seat at all
    {"type": "peer_lost", "hand": 1, "lost_seat": 1},
    # attributable, but names no seat at this table
    {"type": "peer_lost", "hand": 1, "seat": 2, "lost_seat": 7},
    {"type": "peer_lost", "hand": 1, "seat": 2, "lost_seat": True},
], ids=["seat-not-held", "no-reporter", "no-such-seat", "bool-seat"])
def test_a_notice_that_fails_ingress_or_names_no_seat_is_dropped(msg):
    bus, sessions, order = table(3)
    target = sessions["peer1"]
    target.handle_message("peer2", dict(msg))
    assert target.terminal_state is None
