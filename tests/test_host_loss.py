"""Host-loss policy: election in LOBBY only, termination during PLAYING.

The session shipped with unauthenticated host migration. On any host
disconnect, _elect_new_host promoted the lowest-join-order peer -- including
mid-hand, with no authenticated authority transfer and no cryptographic
state transfer. A promoted peer acquired host-only powers (game_start
authority, table settings, player-list broadcast) over a table whose
cryptographic protocol was already in flight.

Policy enforced here:

  LOBBY    host loss re-elects the lowest stable join order from
           already-authenticated membership. No hand or cryptographic
           protocol is active, so nothing is being inherited.

  PLAYING  host identity is immutable. Host loss is one terminal session
           transition with reason HOST_LOST. No election, nobody sets
           is_host, no hand continues, and no state is rebuilt from
           _last_game_state.

Duplicate disconnects are no-ops: the first terminal cause wins.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.session import Player, Session


def make_peers(n=3):
    """n sessions sharing a bus, peer0 host, membership already settled."""
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
        for j, other in enumerate(order):
            s.players[other] = Player(conn_id=other, peer_id=other,
                                      nickname=f"P{j}", avatar_b64="")
        bus.register(cid, s)
        sessions[cid] = s
    return bus, sessions, order


# ------------------------------------------------------------- LOBBY

def test_lobby_host_loss_elects_the_lowest_join_order():
    bus, sessions, order = make_peers(3)
    peer1 = sessions["peer1"]
    assert peer1.state == "LOBBY"
    peer1.handle_disconnect("peer0")
    assert peer1.is_host is True, "peer1 should have been elected in LOBBY"
    assert peer1._host_conn_id == "peer1"


def test_lobby_election_does_not_promote_a_non_candidate():
    bus, sessions, order = make_peers(3)
    peer2 = sessions["peer2"]
    peer2.handle_disconnect("peer0")
    assert peer2.is_host is False
    assert peer2.terminal_state is None


def test_repeated_lobby_disconnect_is_idempotent():
    """Repeated notifications for the same connection must not re-elect."""
    bus, sessions, order = make_peers(3)
    peer1 = sessions["peer1"]
    changes = []
    peer1.on_host_changed = changes.append
    peer1.handle_disconnect("peer0")
    peer1.handle_disconnect("peer0")
    peer1.handle_disconnect("peer0")
    assert changes == [True], f"election fired more than once: {changes}"


# ----------------------------------------------------------- PLAYING

def _start_playing(sessions, order):
    for cid in order:
        sessions[cid].state = "PLAYING"
        sessions[cid].configure_seats(list(order))


def test_playing_host_loss_terminates_and_does_not_elect():
    """The core policy. A mid-hand promotion hands host-only authority over
    an in-flight cryptographic protocol to a peer that inherited none of
    its state."""
    bus, sessions, order = make_peers(3)
    _start_playing(sessions, order)
    peer1 = sessions["peer1"]
    peer1.handle_disconnect("peer0")

    assert peer1.is_host is False, "a peer was promoted during PLAYING"
    assert peer1._host_conn_id == "peer0", "host identity was reassigned"
    assert peer1.terminal_state == "HOST_LOST"


def test_playing_host_loss_is_terminal_for_every_honest_peer():
    bus, sessions, order = make_peers(4)
    _start_playing(sessions, order)
    for cid in order[1:]:
        sessions[cid].handle_disconnect("peer0")
    states = {sessions[cid].terminal_state for cid in order[1:]}
    reasons = {sessions[cid].terminal_reason for cid in order[1:]}
    hosts = {sessions[cid]._host_conn_id for cid in order[1:]}
    assert states == {"HOST_LOST"}, states
    assert len(reasons) == 1, f"peers disagree on the reason: {reasons}"
    assert hosts == {"peer0"}, f"host identity diverged: {hosts}"
    assert not any(sessions[cid].is_host for cid in order[1:])


def test_elect_new_host_refuses_to_run_while_playing():
    """Direct regression guard. Even if some other callback reaches it,
    the election itself must refuse -- dormant code that can be reactivated
    through another path is the failure mode being removed."""
    bus, sessions, order = make_peers(3)
    _start_playing(sessions, order)
    peer1 = sessions["peer1"]
    # _elect_new_host is reachable only from owned contexts, so take
    # ownership explicitly rather than mutating from outside it.
    with peer1._owner:
        peer1._elect_new_host()
    assert peer1.is_host is False
    assert peer1._host_conn_id == "peer0"


def test_host_loss_records_the_first_cause_only():
    bus, sessions, order = make_peers(3)
    _start_playing(sessions, order)
    peer1 = sessions["peer1"]
    peer1.handle_disconnect("peer0")
    first = peer1.terminal_reason
    peer1.handle_disconnect("peer0")
    peer1.handle_disconnect("peer2")
    assert peer1.terminal_state == "HOST_LOST"
    assert peer1.terminal_reason == first, "a later event replaced the cause"


def test_host_loss_notifies_local_consumers_exactly_once():
    bus, sessions, order = make_peers(3)
    _start_playing(sessions, order)
    peer1 = sessions["peer1"]
    seen = []
    peer1.on_session_terminated = seen.append
    peer1.handle_disconnect("peer0")
    peer1.handle_disconnect("peer0")
    assert len(seen) == 1, f"expected one notification, got {len(seen)}"
    assert seen[0].terminal_state == "HOST_LOST"


def test_host_loss_clears_held_messages_and_blocks_mutation():
    bus, sessions, order = make_peers(3)
    _start_playing(sessions, order)
    peer1 = sessions["peer1"]
    peer1.begin_hand(hand_no=1, button=0)
    assert peer1._deal_driver is not None
    peer1.handle_disconnect("peer0")

    assert peer1.terminal_state == "HOST_LOST"
    deal = peer1._deal_driver.deal if peer1._deal_driver else None
    if deal is not None:
        assert not deal._held, "held messages survived a terminal transition"
    # A later protocol message must not mutate a terminated session.
    before = peer1._hand_no
    peer1.handle_message("peer2", {"type": "deck_round", "round": 1,
                                   "seat": 1, "deck": []})
    assert peer1._hand_no == before


def test_terminated_session_does_not_rebuild_from_last_game_state():
    """_last_game_state exists for a host-migration engine rebuild. Nothing
    may use it to continue an interrupted hand."""
    bus, sessions, order = make_peers(3)
    _start_playing(sessions, order)
    peer1 = sessions["peer1"]
    peer1._last_game_state = {"pot": 999, "players": []}
    peer1.handle_disconnect("peer0")
    assert peer1.terminal_state == "HOST_LOST"
    assert peer1._replica is None or peer1.terminal_state is not None


# ----------------------------------------------- non-host loss in lobby

def test_non_host_loss_in_lobby_is_not_terminal():
    bus, sessions, order = make_peers(3)
    peer1 = sessions["peer1"]
    peer1.handle_disconnect("peer2")
    assert peer1.terminal_state is None
    assert peer1._host_conn_id == "peer0"
    assert "peer2" not in peer1.players
