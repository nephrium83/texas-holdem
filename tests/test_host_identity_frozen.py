"""Host identity and terminal state are invariants, not path guarantees.

The host-migration patch gated _elect_new_host and claimed host identity is
frozen during PLAYING. It gated the path it had noticed. _on_player_ack
assigns _host_conn_id from the message sender with no state guard and no
sender check, so any seated peer could relocate another peer's notion of
the host with one unsolicited player_ack -- mid-hand, in the window the
patch declares frozen. Host identity is the authorization token for the
host-gated admin surface (pause, resume, kick, adjust_blinds, session_end),
so hijacking it confers that surface.

These tests are written against the FIELD rather than against a path: no
inbound message of any type may alter host identity or seat identity while
PLAYING. The previous suite asserted _host_conn_id only after a disconnect,
which is why it verified the fix but not the property.

The same shape applies to termination: "terminal sessions ignore protocol
messages" was enforced in handle_message alone, so local entry points could
drive a terminated session back into PLAYING.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.session import Player, Session


def peer(state="PLAYING"):
    bus = InMemoryBus()
    s = Session(is_host=False, nickname="P", avatar_b64="",
                transport=InMemoryTransport(bus, "peer1"),
                master_secret=b"\x02" * 32)
    s.local_conn_id = "peer1"
    s._host_conn_id = "peer0"
    s._join_order = ["peer0", "peer1", "peer2"]
    for cid in s._join_order:
        s.players[cid] = Player(conn_id=cid, peer_id=cid, nickname=cid,
                                avatar_b64="")
    s.state = state
    bus.register("peer1", s)
    return s


# Every message type a hostile peer can send that might touch identity.
HOSTILE = [
    ("player_ack", {"your_conn_id": "peer1"}),
    ("player_ack", {"your_conn_id": "ZZZ"}),
    ("player_ack", {}),
    ("player_list", {"players": [{"conn_id": "attacker", "is_host": True}]}),
    ("game_start", {"seat_order": ["attacker", "peer1"],
                    "table_settings": {}}),
    ("ready", {"ready": True}),
    ("chat", {"nickname": "x", "text": "y"}),
]


# ------------------------------------------------- the invariant itself

@pytest.mark.parametrize("mtype,payload", HOSTILE)
def test_no_message_can_reassign_host_identity_while_playing(mtype, payload):
    s = peer("PLAYING")
    s.handle_message("attacker", {"type": mtype, "payload": payload})
    assert s._host_conn_id == "peer0", (
        f"{mtype} relocated host identity to the sender")
    assert s.is_host is False, f"{mtype} granted host authority"


@pytest.mark.parametrize("mtype,payload", HOSTILE)
def test_no_message_can_repoint_local_seat_identity_while_playing(
        mtype, payload):
    """local_conn_id feeds the seat-spoof check and _deal_session_id."""
    s = peer("PLAYING")
    s.handle_message("attacker", {"type": mtype, "payload": payload})
    assert s.local_conn_id == "peer1", f"{mtype} repointed seat identity"


def test_hijacked_host_cannot_gain_admin_authority():
    """The consequence that makes this a privilege escalation rather than a
    bookkeeping error."""
    s = peer("PLAYING")
    paused = []
    s.on_pause = lambda: paused.append(1)
    s.handle_message("attacker", {"type": "player_ack",
                                  "payload": {"your_conn_id": "peer1"}})
    s.handle_message("attacker", {"type": "pause", "payload": {}})
    assert not paused, "attacker acquired host-gated admin authority"


def test_player_ack_assigns_our_id_but_no_longer_elects_the_host():
    """The legitimate use survives; the dangerous side effect does not.

    player_ack used to ALSO set _host_conn_id when the host was unknown,
    because it was historically how a joiner learned who the host was. That
    made "first peer to speak" the election rule, and host identity gates
    pause, resume, kick, adjust_blinds, game_start and session_end.
    wire.unpack proves only that a message was signed by SOME key, so the
    election was decided by transmission order.

    Learning our own conn_id from it is still fine. Deciding who the host
    is now happens in exactly one place -- mark_host_authenticated(), after
    a signed admission_accept verifies against the exact key the invite
    pinned. See tests/test_admission.py.
    """
    s = peer("LOBBY")
    s._host_conn_id = ""                    # host not yet known
    s.handle_message("peer0", {"type": "player_ack",
                               "payload": {"your_conn_id": "peer1"}})
    assert s.local_conn_id == "peer1", "the joiner must still learn its own id"
    assert s._host_conn_id == "", (
        "player_ack elected a host; only a verified admission_accept may")


def test_player_ack_from_a_non_host_is_refused_in_the_lobby():
    s = peer("LOBBY")
    s.handle_message("attacker", {"type": "player_ack",
                                  "payload": {"your_conn_id": "peer1"}})
    assert s._host_conn_id == "peer0"
    assert s.local_conn_id == "peer1"


def test_player_ack_with_a_malformed_conn_id_is_refused():
    s = peer("LOBBY")
    s._host_conn_id = ""
    for bad in ("", None, 7, ["peer1"]):
        s.handle_message("peer0", {"type": "player_ack",
                                   "payload": {"your_conn_id": bad}})
        assert s.local_conn_id == "peer1", f"accepted {bad!r}"


# ------------------------------------------ terminal state vs local calls

def test_start_game_is_refused_after_termination():
    s = peer("PLAYING")
    s.is_host = True
    s.terminate(Session.HOST_LOST, "host dropped")
    with pytest.raises(RuntimeError, match="terminated"):
        s.start_game({"bg_prevention": False})
    assert s.state == "ENDED"
    assert s.terminal_state == Session.HOST_LOST


def test_begin_hand_is_refused_after_termination():
    s = peer("PLAYING")
    s.configure_seats(["peer0", "peer1", "peer2"])
    s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
    s.terminate(Session.HOST_LOST, "host dropped")
    with pytest.raises(RuntimeError, match="terminated"):
        s.begin_hand(hand_no=5, button=0)
    assert s._deal_driver is None
    assert s._hand_no == 0


def test_next_hand_is_refused_after_termination():
    s = peer("PLAYING")
    s.configure_seats(["peer0", "peer1", "peer2"])
    s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
    s.terminate(Session.HOST_LOST, "host dropped")
    assert s.next_p2p_hand() == "session_over"


def test_termination_survives_a_local_restart_attempt():
    """The contradictory state machine the review found: a terminated
    session driven back into PLAYING while terminal_state still reads
    HOST_LOST."""
    s = peer("PLAYING")
    s.is_host = True
    s.terminate(Session.HOST_LOST, "host dropped")
    for attempt in (lambda: s.start_game({}), lambda: s.begin_hand(1, 0)):
        try:
            attempt()
        except RuntimeError:
            pass
    assert s.state == "ENDED"
    assert s.terminal_state == Session.HOST_LOST
    assert s._deal_driver is None


# ------------------------------------- freeze keyed on terminality

def test_capability_freeze_does_not_lapse_after_termination():
    """_on_game_start's freeze was conditioned on state == "PLAYING", and
    terminate() sets state to ENDED -- so the freeze silently stopped
    applying, masked only by the handle_message guard."""
    s = peer("PLAYING")
    # Installed through the single writer, not by assigning the field. A
    # direct write would set up state the chokepoint could never produce,
    # leaving this test proving only that the freeze branch returns early.
    assert s._adopt_deal_policy(Session.DEAL_POLICY_BG)
    s.terminate(Session.HOST_LOST, "host dropped")
    s._on_game_start("peer0", {"payload": {
        "seat_order": ["attacker", "peer1"],
        "table_settings": {
            Session.DEAL_POLICY_SETTING: Session.DEAL_POLICY_DETECTION}}})
    assert s.prevention is True, "prevention downgraded after termination"
    assert s.deal_policy == Session.DEAL_POLICY_BG
    assert s.state == "ENDED"
