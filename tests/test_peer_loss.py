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

The leaver folds (docs/CASUAL_P2P_RULES.md). A hand it was dealt into
stops, and every game still playing settles its own copy with
settle_forfeit: the leaver forfeits what it put in and the seats left
share each pot. A hand not yet dealt is cancelled and its blinds go back.
The tables here run Bayer-Groth, as every networked table does, because a
hand settles without its end-of-hand card check only on proofs. Chips
stand at last_settled_stacks.

A busted host cannot judge a drop -- its replica stopped at the hand it
busted in -- so it only reports one, and the seats still playing decide.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem import client_view
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


def table(n=3, stacks=None, start=True, policy=Session.DEAL_POLICY_BG):
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
        s._adopt_deal_policy(policy)
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
    """Heads-up, seat 1 completes its small blind to 10 and drops. It
    folds: its 10 stays in the pot, and seat 0 takes the 20."""
    bus, sessions, order = table(2)
    host = sessions["peer0"]
    act(bus, sessions, order)                    # chips into the pot
    assert host.replica.stacks == [490, 490]
    seen = []
    host.on_session_terminated = seen.append

    host.handle_disconnect("peer1")

    assert host.terminal_state == Session.PEER_LOST
    assert host.terminal_reason == (
        "seat 1 (P1) disconnected; it forfeits hand 1")
    assert host.terminal_record.initiating_seat == 1
    assert host.terminal_record.conn_id == "peer1"
    assert host.hand_record.outcome == Session.HAND_FORFEIT
    assert host.hand_record.blamed_seat == 1
    assert len(seen) == 1
    assert host.last_settled_stacks == [510, 490]
    # The client is not told its chips were restored: they were not.
    snap = client_view.snapshot(host)
    assert snap["turn"]["headline"] == "Hand stopped | P1 dropped and forfeits"
    assert snap["void_reason"] == "seat 1 (P1) dropped and forfeits hand 1"


def test_peer_lost_in_a_later_hand_forfeits_that_hand(monkeypatch):
    """Hand 1 settles with seat 0 ahead. In hand 2 seat 0 posts the small
    blind, seat 1 the big, and seat 2 calls 10 from the button, then
    drops. Its 10 is forfeit: seat 0's 5 and 5 from each other seat make
    15, shared by seats 0 and 1 with the odd chip to seat 0, first left
    of the button; seat 1's other 5 and seat 2's make 10, seat 1's alone.
    """
    rig_showdowns(monkeypatch)(0, 1, 2)
    bus, sessions, order = table(3)
    host = sessions["peer0"]
    settle_by_checkdown(bus, sessions, order)
    settled = host.replica.stacks
    assert settled == [520, 490, 490]            # seat 0 took 3 x 10
    assert host.last_settled_stacks == settled
    assert {sessions[c].next_p2p_hand() for c in order} == {"started"}
    bus.drain()
    act(bus, sessions, order)
    assert host.replica.stacks == [515, 480, 480]

    host.handle_disconnect("peer2")

    assert host.terminal_state == Session.PEER_LOST
    assert host.terminal_reason == (
        "seat 2 (P2) disconnected; it forfeits hand 2")
    assert host.last_settled_stacks == [523, 497, 480]


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


def test_a_drop_from_a_table_without_proofs_settles_nothing():
    """A hand settled on a drop skips its end-of-hand card check, so it
    needs Bayer-Groth proofs. A detection-only table has none, and the
    transport is not asked: it is compat here, as on the sidecar. The
    hand is not settled, and chips stand where it was dealt from."""
    bus, sessions, order = table(2, policy=Session.DEAL_POLICY_DETECTION)
    host = sessions["peer0"]
    act(bus, sessions, order)
    assert host.replica.stacks == [490, 490]

    host.handle_disconnect("peer1")

    assert host.terminal_state == Session.PEER_LOST
    assert host.terminal_reason == (
        "seat 1 (P1) disconnected; hand 1 was not settled: this table runs "
        "no Bayer-Groth shuffle proofs")
    assert host.hand_record is None
    assert host.last_settled_stacks == [500, 500]


def test_a_game_that_did_not_verify_every_proof_settles_nothing():
    """The deal aborts on a shuffle round without a valid proof, so a
    dealt Bayer-Groth hand has one per seat. The drop path counts them
    itself rather than lean on that: this game is made to have verified
    one fewer than the hand's three rounds, and it settles nothing, while
    the seat that verified all three forfeits the leaver."""
    bus, sessions, order = table(3)
    host, other = sessions["peer0"], sessions["peer1"]
    act(bus, sessions, order)
    assert host.proofs_verified == other.proofs_verified == 3
    host._deal_driver.deal._proofs_verified -= 1
    bus.unregister("peer2")

    host.handle_disconnect("peer2")
    bus.drain()

    assert host.terminal_reason == (
        "seat 2 (P2) disconnected; hand 1 was not settled: this game "
        "verified 2 of its 3 shuffle proofs")
    assert host.last_settled_stacks == [500, 500, 500]
    assert other.terminal_reason.endswith("; it forfeits hand 1")
    assert other.last_settled_stacks == [503, 502, 495]


def test_a_voided_hands_seats_are_still_needed_for_the_redeal():
    bus, sessions, order = table(3)
    host = sessions["peer0"]
    for cid in order:
        sessions[cid]._void_hand("protocol failure")
    bus.drain()
    bus.unregister("peer1")
    host.handle_disconnect("peer1")
    bus.drain()
    assert host.terminal_state == Session.PEER_LOST
    assert host.last_settled_stacks == [500, 500, 500]
    # A voided hand is redealt from where it was dealt from, as a
    # cancelled one is, so the notice calls it not dealt and the other
    # survivor, which voided it too, agrees.
    assert sessions["peer2"].terminal_reason == (
        "seat 1 (P1) disconnected (reported by seat 0)")
    assert sessions["peer2"].last_settled_stacks == [500, 500, 500]


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

def peer_lost_sent(session):
    """Record the peer_lost notices ``session`` sends, as sent."""
    sent = []
    broadcast = session._transport.broadcast

    def record(msg):
        if msg.get("type") == "peer_lost":
            sent.append({k: msg.get(k) for k in (
                "hand", "lost_seat", "ended", "dealt", "settled", "actions")})
        broadcast(msg)
    session._transport.broadcast = record
    return sent


def test_every_survivor_ends_when_one_seat_drops():
    """Only the host's socket closes when a joiner drops (the production
    graph is a star). The host's signed peer_lost notice is how the other
    joiner finds out; without it that joiner waits forever. The notice
    carries no figures, only where the host's hand stood, and the joiner
    settles its own copy of the table. Seat 2 forfeits its small blind:
    seats 0 and 1 share it, the odd chip to seat 0, first left of the
    button."""
    bus, sessions, order = table(3)
    host, other = sessions["peer0"], sessions["peer1"]
    act(bus, sessions, order)
    assert host.replica.stacks == [490, 490, 495]
    sent = peer_lost_sent(host)
    bus.unregister("peer2")                      # the process is gone

    host.handle_disconnect("peer2")
    bus.drain()

    assert sent == [{"hand": 1, "lost_seat": 2, "ended": True,
                     "dealt": True, "settled": False, "actions": 1}]
    assert other.terminal_state == Session.PEER_LOST
    assert other.terminal_reason == (
        "seat 2 (P2) disconnected (reported by seat 0); it forfeits hand 1")
    assert other.terminal_record.initiating_seat == 2
    assert other.last_settled_stacks == host.last_settled_stacks \
        == [503, 502, 495]


def test_an_action_that_races_the_drop_is_disputed():
    """Seat 2 folds its small blind, and on the flop seat 0 bets 20. Seat
    1 folds in the same instant as seat 2 drops: the fold is on its way
    to the host when the host ends the table, so the host never applies
    it. Seat 1's game has applied one action more than the notice says.
    Each game shows its own result: the host's has seats 0 and 1 share
    the pot, seat 1's has seat 0 win it, and seat 1's is marked disputed
    (rule 11) rather than guess at the host's."""
    bus, sessions, order = table(3)
    host, other = sessions["peer0"], sessions["peer1"]
    act(bus, sessions, order)                    # seat 1 calls
    act(bus, sessions, order, "fold")            # seat 2 folds
    act(bus, sessions, order)                    # seat 0 checks
    assert host.replica.engine.street == "flop"
    assert host.replica.actor == 0
    assert host.send_bet_action("raise", 20) == "applied"
    bus.drain()
    assert other.send_bet_action("fold") == "applied"   # still in flight
    bus.unregister("peer2")

    host.handle_disconnect("peer2")
    bus.drain()

    # Seat 0's uncalled 20 comes back; the 25 left is shared.
    assert host.terminal_reason == (
        "seat 2 (P2) disconnected; it forfeits hand 1")
    assert host.last_settled_stacks == [503, 502, 495]
    assert other.terminal_state == Session.PEER_LOST
    assert other.terminal_reason == (
        "seat 2 (P2) disconnected (reported by seat 0); it forfeits hand 1; "
        "disputed: seat 0 had applied 4 actions in hand 1, this game "
        "applied 5 actions in hand 1")
    assert other.last_settled_stacks == [515, 490, 495]


def test_a_survivor_a_hand_ahead_cancels_the_hand_it_is_shuffling():
    """Hands begin when each peer calls next_p2p_hand, so the reporter can
    still be on the last hand while a survivor has begun the next one. The
    notice is not hand-scoped: the table is over either way. That hand
    cannot have been dealt, since the reporter's shuffle round is missing,
    so the survivor cancels it and the blinds it posted go back (rule 5).
    """
    bus, sessions, order = table(3)
    settle_by_checkdown(bus, sessions, order)
    settled = sessions["peer0"].replica.stacks
    ahead = sessions["peer1"]
    assert ahead.next_p2p_hand() == "started"
    bus.drain()
    assert ahead._hand_no == 2 and sessions["peer0"]._hand_no == 1
    assert ahead.replica.stacks != settled       # hand 2's blinds
    bus.unregister("peer2")

    sessions["peer0"].handle_disconnect("peer2")
    bus.drain()

    assert ahead.terminal_state == Session.PEER_LOST
    assert ahead.terminal_reason == (
        "seat 2 (P2) disconnected (reported by seat 0); hand 2 was "
        "cancelled before the deal and its blinds go back")
    assert ahead.hand_record.outcome == Session.VOID_PEER_LOST
    assert ahead.last_settled_stacks == settled


def test_a_drop_while_the_next_hand_is_shuffled_cancels_it(monkeypatch):
    """Hand 1 settles with seat 2 ahead. Seats 0 and 1 press Next, post
    hand 2's blinds and start shuffling it. Seat 2 drops before it
    presses Next, so the shuffle never finishes and nobody has seen a
    card. The hand is cancelled, not forfeit: the blinds go back (rule 5),
    and seat 2 keeps what it won in hand 1."""
    rig_showdowns(monkeypatch)(2, 0, 1)
    bus, sessions, order = table(3)
    settle_by_checkdown(bus, sessions, order)
    settled = sessions["peer0"].replica.stacks
    assert settled == [490, 490, 520]
    for c in order[:2]:
        assert sessions[c].next_p2p_hand() == "started"
    bus.drain()
    host = sessions["peer0"]
    assert host.replica.stacks == [485, 480, 520]
    assert not host._deal_driver.deal.is_shuffle_complete()
    sent = peer_lost_sent(host)
    bus.unregister("peer2")

    host.handle_disconnect("peer2")
    bus.drain()

    assert sent == [{"hand": 2, "lost_seat": 2, "ended": True,
                     "dealt": False, "settled": False, "actions": 0}]
    for c in order[:2]:
        s = sessions[c]
        assert s.terminal_state == Session.PEER_LOST, c
        assert s.terminal_reason.endswith(
            "; hand 2 was cancelled before the deal and its blinds go "
            "back"), c
        assert s.hand_record.outcome == Session.VOID_PEER_LOST, c
        assert s.last_settled_stacks == settled, c


def test_a_deal_one_game_finished_alone_is_cancelled_with_the_rest():
    """Seat 2 shuffles last. Its own round finishes the shuffle in its own
    game at once, but the round is still on its way when seat 1 drops, so
    the host's notice says the hand was not dealt. Seat 2 follows the
    notice and cancels the hand as the host does, rather than forfeit
    seat 1's blind on a deal no other game ever saw finish."""
    bus, sessions, order = table(3, start=False)
    last = sessions["peer2"]
    held = []
    broadcast = last._transport.broadcast
    last._transport.broadcast = lambda msg: (
        held.append(msg) if msg.get("type") == "deck_round"
        and msg.get("round") == 3 else broadcast(msg))
    for cid in order:
        sessions[cid].start_p2p_hand(
            hand_no=1, names=["P0", "P1", "P2"], stacks=[500] * 3,
            sb=5, bb=10, button=0)
    bus.drain()
    host = sessions["peer0"]
    assert len(held) == 1
    assert last._deal_driver.deal.is_shuffle_complete()
    assert last.proofs_verified == 3
    assert not host._deal_driver.deal.is_shuffle_complete()
    bus.unregister("peer1")

    host.handle_disconnect("peer1")
    bus.drain()

    assert last.terminal_reason == (
        "seat 1 (P1) disconnected (reported by seat 0); hand 1 was "
        "cancelled before the deal and its blinds go back")
    assert last.last_settled_stacks == host.last_settled_stacks \
        == [500, 500, 500]


def test_a_hand_the_reporter_voided_is_cancelled_by_a_game_that_did_not():
    """The host has voided hand 1, which redeals it from the stacks it was
    dealt from, as cancelling it would; seat 2 has not heard. The host's
    notice calls a voided hand not dealt, so seat 2 cancels it too and
    both games end on the same stacks, instead of seat 2 forfeiting seat
    1's blind on a hand the host had already given up."""
    bus, sessions, order = table(3)
    host, last = sessions["peer0"], sessions["peer2"]
    act(bus, sessions, order)
    host._void_hand("protocol failure", announce=False)
    bus.unregister("peer1")

    host.handle_disconnect("peer1")
    bus.drain()

    assert host.last_settled_stacks == [500, 500, 500]
    assert last.terminal_reason == (
        "seat 1 (P1) disconnected (reported by seat 0); hand 1 was "
        "cancelled before the deal and its blinds go back")
    assert last.last_settled_stacks == [500, 500, 500]


def test_a_settlement_report_that_crosses_the_drop_is_disputed():
    """Seat 2's report that it settled hand 1 reaches seat 1 before seat
    1 has settled it, and then seat 2 drops. The report is the race rule
    11 covers: seat 1 keeps its own result, the forfeit, and marks it
    disputed, rather than end the table over the report and roll the
    hand back."""
    bus, sessions, order = table(3)
    other = sessions["peer1"]
    act(bus, sessions, order)
    other.handle_message("peer2", {"type": "hand_settled", "hand": 1,
                                   "seat": 2, "digest": "ab" * 32})
    assert other.terminal_state is None
    bus.unregister("peer2")

    sessions["peer0"].handle_disconnect("peer2")
    bus.drain()

    assert other.terminal_state == Session.PEER_LOST
    assert other.terminal_reason == (
        "seat 2 (P2) disconnected (reported by seat 0); it forfeits hand 1; "
        "disputed: seat 2 had settled hand 1, this game had not")
    assert other.last_settled_stacks == [503, 502, 495]


def test_a_winner_everyone_folded_to_is_paid_when_it_drops():
    """Seat 1 completes its small blind and seat 0 folds its big blind.
    The hand is over, waiting only on the end-of-hand card check, when
    seat 1 drops without sending its share. Betting has decided the hand,
    so seat 1 is paid the pot (decision 1 of the approved plan) rather
    than forfeiting it."""
    bus, sessions, order = table(2)
    host = sessions["peer0"]
    act(bus, sessions, order)                    # seat 1 calls
    bus.unregister("peer1")                      # it never sees the fold
    assert host.send_bet_action("fold") == "applied"
    bus.drain()
    assert host.hand_result is None              # the check waits on seat 1

    host.handle_disconnect("peer1")

    assert host.terminal_reason == (
        "seat 1 (P1) disconnected; everyone else had folded hand 1, so it "
        "is paid the pot")
    assert host.hand_result is not None
    assert host.last_settled_stacks == [490, 510]


def test_a_notice_that_does_not_say_where_its_hand_stood_is_disputed():
    """A host-signed notice without the hand's place still ends the table,
    since the host relays nothing more, but no game can tell whether it is
    where the host was. Each settles its own copy and marks it disputed."""
    bus, sessions, order = table(3)
    other = sessions["peer1"]
    act(bus, sessions, order)
    other.handle_message("peer0", {"type": "peer_lost", "hand": 1,
                                   "seat": 0, "lost_seat": 2,
                                   "ended": True})
    assert other.terminal_state == Session.PEER_LOST
    assert other.terminal_reason == (
        "seat 2 (P2) disconnected (reported by seat 0); it forfeits hand 1; "
        "disputed: seat 0's notice did not say where its hand stood")
    assert other.last_settled_stacks == [503, 502, 495]


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


@pytest.mark.parametrize("dealt", [False, True],
                         ids=["claims-not-dealt", "claims-dealt"])
def test_a_confirmation_moves_no_chips_in_a_hand_being_played(monkeypatch,
                                                              dealt):
    """The host has reported seat 3, which nobody needed, and seats 1 and
    2 have dealt hand 3 and put chips in. Seat 1 then confirms the stale
    report, with whatever stage it likes. An honest confirmer needed
    seat 3, so seat 2 could not have dealt a hand without it, and the
    notice is no confirmation: seat 2 neither cancels the hand ("not
    dealt", its blinds back) nor stops it and splits the pot. The busted
    host cannot judge, and still ends."""
    bus, sessions, order = busted_host_table(monkeypatch)
    bus.unregister("peer3")
    sessions["peer0"].handle_disconnect("peer3")
    bus.drain()
    assert {sessions[c].next_p2p_hand() for c in order[1:3]} == {"started"}
    bus.drain()
    other = sessions["peer2"]
    actor = other.replica.actor
    assert sessions[order[actor]].send_bet_action("call") == "applied"
    bus.drain()
    stacks = list(other.replica.stacks)
    assert stacks == [0, 615, 485, 0]

    sessions["peer1"]._send_hostless({
        "type": "peer_lost", "hand": 3, "lost_seat": 3, "ended": True,
        "dealt": dealt, "settled": False,
        "actions": other.replica.next_seq})
    bus.drain()

    assert other.terminal_state is None
    assert other.hand_result is None and not other.hand_voided
    assert other.replica.stacks == stacks
    assert other.last_settled_stacks == [0, 625, 495, 0]
    assert sessions["peer0"].terminal_state == Session.PEER_LOST


def test_a_confirmation_between_hands_ends_the_table_and_moves_no_chips(
        monkeypatch):
    """Hand 2 is settled and hand 3 not begun: a confirmer that needed the
    reported seat ends the table there, and the settled stacks stand."""
    bus, sessions, order = busted_host_table(monkeypatch)
    bus.unregister("peer3")
    sessions["peer0"].handle_disconnect("peer3")
    bus.drain()

    sessions["peer1"]._send_hostless({
        "type": "peer_lost", "hand": 2, "lost_seat": 3, "ended": True,
        "dealt": True, "settled": False, "actions": 3})
    bus.drain()

    other = sessions["peer2"]
    assert other.terminal_state == Session.PEER_LOST
    assert other.terminal_reason == (
        "seat 3 (P3) disconnected (reported by seat 1); disputed: seat 1 "
        "had applied 3 actions in hand 2, this game settled hand 2")
    assert other.last_settled_stacks == [0, 625, 495, 0]


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
    confirmed = peer_lost_sent(sessions["peer1"])
    bus.unregister("peer2")

    sessions["peer0"].handle_disconnect("peer2")
    bus.drain()

    for c in ("peer0", "peer1", "peer3"):
        assert sessions[c].terminal_state == Session.PEER_LOST, c
        assert sessions[c].terminal_record.initiating_seat == 2
    # The confirmation says where seat 1's hand stood, as the host's own
    # notice would have.
    assert confirmed == [{"hand": 3, "lost_seat": 2, "ended": True,
                          "dealt": True, "settled": False, "actions": 1}]
    # Seat 1 settles its own copy: heads-up with seat 2 in hand 3, both
    # had 10 in, and seat 2's is forfeit.
    assert sessions["peer1"].terminal_reason == (
        "seat 2 (P2) disconnected (reported by seat 0); it forfeits hand 3")
    # The host dropped seat 2 from its roster when the socket closed, so
    # by the time the confirmation names it, it has no nickname there.
    assert sessions["peer0"].terminal_reason == (
        "seat 2 disconnected (reported by seat 1)")
    assert sessions["peer3"].terminal_reason == (
        "seat 2 (P2) disconnected (reported by seat 1)")
    assert sessions["peer1"].last_settled_stacks == [0, 635, 485, 0]
    # The busted seats stopped following hands, so they settle nothing
    # and keep the figure they busted with.
    assert sessions["peer0"].last_settled_stacks == [0, 535, 495, 90]
    assert sessions["peer3"].last_settled_stacks == [0, 625, 495, 0]


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
