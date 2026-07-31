"""Terminal state is absorbing, across the whole Session mutation surface.

Written property-shaped rather than path-shaped, deliberately. Three defects
on this branch shared one cause: an invariant enforced at the paths someone
thought of rather than at every writer.

  * the shuffle argument bound the decks on the path it checked
  * host identity was frozen against disconnects but not against player_ack
  * termination blocked start_game, begin_hand and next_p2p_hand -- and
    missed send_bet_action, which not only mutated the local replica but
    BROADCAST the action into a table the peer had already left

So the sweep below enumerates every externally callable method that can
mutate protocol state and asserts that none of them changes anything after
termination. Adding a method to Session without guarding it fails here
rather than surviving until someone audits that particular path.

Two levels of termination exist and are deliberately different:

  session (terminate)   ABSORBING -- nothing may mutate afterwards
  hand    (_end_hand)   RECOVERABLE -- a voided hand is redealt to the same
                        seats at the same button, so it must NOT make the
                        session terminal
"""
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.session import Player, Session


def table(n=3, stacks=None, hand_no=1):
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
        for c in order:
            s.players[c] = Player(conn_id=c, peer_id=c, nickname=c,
                                  avatar_b64="")
        s.configure_seats(list(order))
        s.state = "PLAYING"          # a table dealing a hand is PLAYING
        bus.register(cid, s)
        sessions[cid] = s
    for cid in order:
        sessions[cid].start_p2p_hand(
            hand_no=hand_no, names=[f"P{i}" for i in range(n)],
            stacks=list(stacks or [500] * n), sb=5, bb=10, button=0)
    bus.drain()
    return bus, sessions, order


def snapshot(s):
    """Everything a terminated session must hold still."""
    return {
        "state": s.state,
        "terminal_state": s.terminal_state,
        "terminal_reason": s.terminal_reason,
        "terminal_record": s.terminal_record,
        "seat_order": list(s._seat_order),
        "hand_no": s._hand_no,
        "prevention": s._prevention,
        "host_conn_id": s._host_conn_id,
        "local_conn_id": s.local_conn_id,
        "replica_digest": (s._replica.state_digest()
                           if s._replica is not None else None),
        "hand_record": s._hand_record,
        "held": (len(s._deal_driver.deal._held)
                 if s._deal_driver is not None else 0),
        "msg_buffer": len(s._msg_buffer),
        "deadline": s._current_deadline_token,
        "outbox": len(s._deal_outbox),
    }


# Every externally callable method that could mutate protocol state, with
# valid-shaped and hostile arguments.
MUTATORS = [
    ("send_bet_action", lambda s: s.send_bet_action("call", 0)),
    ("send_bet_action/raise", lambda s: s.send_bet_action("raise", 10 ** 9)),
    ("send_bet_action/negative", lambda s: s.send_bet_action("raise", -5)),
    ("begin_hand", lambda s: s.begin_hand(hand_no=99, button=0)),
    ("next_p2p_hand", lambda s: s.next_p2p_hand()),
    ("start_game", lambda s: s.start_game({"bg_prevention": False})),
    ("start_p2p_hand", lambda s: s.start_p2p_hand(
        hand_no=42, names=["A", "B", "C"], stacks=[9, 9, 9], sb=1, bb=2,
        button=0)),
    ("reveal_board_street", lambda s: s.reveal_board_street("flop")),
    ("open_deal_audit", lambda s: s.open_deal_audit()),
    ("configure_seats", lambda s: s.configure_seats(["x", "y"])),
    ("add_local_player", lambda s: s.add_local_player("intruder")),
    ("set_ready", lambda s: s.set_ready("peer2", True)),
    ("check_deadlines", lambda s: s.check_deadlines()),
    ("broadcast_game_state", lambda s: s.broadcast_game_state()),
    ("handle_disconnect", lambda s: s.handle_disconnect("peer2")),
    ("handle_disconnect/host", lambda s: s.handle_disconnect("peer0")),
    ("handle_game_action", lambda s: s.handle_game_action(
        "peer2", {"type": "action", "action": "raise", "amount": 10 ** 9})),
    ("_void_hand", lambda s: s._void_hand("late void")),
    ("terminate/other", lambda s: s.terminate(
        Session.ABORTED_PROTOCOL, "second cause")),
]

HOSTILE_MESSAGES = [
    {"type": "bet_action", "hand": 1, "seq": 0, "seat": 1,
     "action": "raise", "amount": 10 ** 9},
    {"type": "hand_void", "hand": 1, "seat": 2, "reason": "forged"},
    {"type": "session_end", "hand": 1, "stacks": [0, 0, 1500]},
    {"type": "timeout_proposal", "hand": 1,
     "token": {"hand_id": "x", "phase": "betting", "actor": 1,
               "action_seq": 0}},
    {"type": "game_start", "payload": {"seat_order": ["evil"],
                                       "table_settings": {}}},
    {"type": "player_ack", "payload": {"your_conn_id": "evil"}},
    {"type": "deck_round", "round": 1, "seat": 1, "deck": []},
]


# ------------------------------------------------- the absorbing property

@pytest.mark.parametrize("name,call", MUTATORS,
                         ids=[m[0] for m in MUTATORS])
def test_no_local_entry_point_mutates_after_termination(name, call):
    bus, sessions, order = table()
    s = sessions["peer1"]
    s.is_host = True                    # widen the reachable surface
    s.terminate(Session.HOST_LOST, "host dropped")
    before = snapshot(s)
    try:
        call(s)
    except RuntimeError:
        pass                            # refusing loudly is acceptable
    assert snapshot(s) == before, f"{name} mutated a terminated session"


@pytest.mark.parametrize("msg", HOSTILE_MESSAGES,
                         ids=[m["type"] for m in HOSTILE_MESSAGES])
def test_no_inbound_message_mutates_after_termination(msg):
    bus, sessions, order = table()
    s = sessions["peer1"]
    s.terminate(Session.HOST_LOST, "host dropped")
    before = snapshot(s)
    s.handle_message("peer2", dict(msg))
    assert snapshot(s) == before, f"{msg['type']} mutated a terminated session"


def test_send_bet_action_after_termination_is_inert():
    """Dedicated regression for the audit blocker. Before the guard this
    returned "applied", changed the replica digest, AND broadcast the action
    to peers -- injecting into a table this peer had already left."""
    bus, sessions, order = table()
    s = sessions["peer1"]
    s.terminate(Session.HOST_LOST, "host dropped")
    digest_before = s._replica.state_digest()
    sent_before = len(bus._queue)

    verdict = s.send_bet_action("call", 0)

    assert verdict == "rejected"
    assert s._replica.state_digest() == digest_before
    assert len(bus._queue) == sent_before, "a terminated peer broadcast"


def test_termination_is_absorbing_under_a_full_sweep():
    """Every mutator, in sequence, against one terminated session."""
    bus, sessions, order = table()
    s = sessions["peer1"]
    s.is_host = True
    s.terminate(Session.HOST_LOST, "host dropped")
    before = snapshot(s)
    for _name, call in MUTATORS:
        try:
            call(s)
        except RuntimeError:
            pass
    for msg in HOSTILE_MESSAGES:
        s.handle_message("peer2", dict(msg))
    assert snapshot(s) == before


# ------------------------------------------------ competing terminal causes

def test_void_then_host_loss_keeps_both_levels_distinct():
    """A void is recoverable and must not pre-empt session termination."""
    bus, sessions, order = table()
    s = sessions["peer1"]
    s._void_hand("protocol failure")
    assert s.hand_voided
    assert s.terminal_state is None, "a hand void terminated the session"
    s.handle_disconnect("peer0")
    assert s.terminal_state == Session.HOST_LOST


def test_host_loss_then_void_leaves_the_first_cause_intact():
    bus, sessions, order = table()
    s = sessions["peer1"]
    s.handle_disconnect("peer0")
    record = s.terminal_record
    assert s._void_hand("late void") is False, "voided a terminated session"
    assert s.terminal_record is record
    assert s.terminal_state == Session.HOST_LOST


def test_normal_end_then_protocol_abort_keeps_the_first_cause():
    bus, sessions, order = table()
    s = sessions["peer1"]
    assert s.terminate(Session.ENDED_NORMAL, "match complete") is True
    first = s.terminal_record
    assert s.terminate(Session.ABORTED_PROTOCOL, "later abort") is False
    assert s.terminal_record is first
    assert s.terminal_state == Session.ENDED_NORMAL


def test_local_shutdown_racing_an_inbound_terminal_message():
    bus, sessions, order = table()
    s = sessions["peer1"]
    assert s.terminate(Session.LOCAL_SHUTDOWN, "user quit") is True
    s.handle_message("peer0", {"type": "session_end", "hand": 1,
                               "stacks": [1500, 0, 0]})
    assert s.terminal_state == Session.LOCAL_SHUTDOWN


def test_repeated_identical_terminal_requests_are_idempotent():
    bus, sessions, order = table()
    s = sessions["peer1"]
    seen = []
    s.on_session_terminated = seen.append
    for _ in range(10):
        s.terminate(Session.HOST_LOST, "host dropped")
    assert len(seen) == 1
    assert s.terminal_record.sequence == 1


def test_concurrent_terminal_requests_yield_one_winner():
    """Under the current single-writer model these arrive serialized; the
    mechanism should not depend on that to stay correct."""
    # A fresh table per trial would run a full mental-poker deal each time;
    # the race is in terminate() alone, so one table is reused and only the
    # terminal fields are reset between trials.
    bus, sessions, order = table()
    s = sessions["peer1"]
    for _ in range(500):
        s.terminal_state = None
        s.terminal_reason = None
        s.terminal_record = None
        seen = []
        s.on_session_terminated = seen.append
        # Barrier counts the worker threads only -- the main thread does not
        # participate, or it would wait for a party that never arrives.
        gate = threading.Barrier(3)

        def fire(state, reason):
            gate.wait()
            s.terminate(state, reason)

        threads = [
            threading.Thread(target=fire, args=(Session.HOST_LOST, "A")),
            threading.Thread(target=fire, args=(Session.ABORTED_PROTOCOL, "B")),
            threading.Thread(target=fire, args=(Session.LOCAL_SHUTDOWN, "C")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(seen) == 1, f"{len(seen)} notifications"
        assert s.terminal_record is seen[0]


# ------------------------------------------------------ hand level stays sane

def test_hand_void_is_recoverable_and_redeals():
    """The reason hand termination is NOT routed through terminate()."""
    bus, sessions, order = table()
    for cid in order:
        sessions[cid]._void_hand("protocol failure")
    bus.drain()
    verdicts = {cid: sessions[cid].next_p2p_hand() for cid in order}
    bus.drain()
    assert set(verdicts.values()) == {"started"}
    for cid in order:
        assert sessions[cid].terminal_state is None
        assert sessions[cid].hand_voided is False, "void survived the redeal"
        assert sessions[cid]._hand_no == 2


def test_hand_void_produces_exactly_one_record():
    bus, sessions, order = table()
    s = sessions["peer1"]
    assert s._void_hand("first") is True
    record = s.hand_record
    assert s._void_hand("second") is False
    assert s._void_hand("third") is False
    assert s.hand_record is record
    assert record.reason == "first"
    assert record.sequence == 1


def test_hand_voided_is_derived_not_assignable():
    """It must not be a second source of termination truth."""
    bus, sessions, order = table()
    s = sessions["peer1"]
    with pytest.raises(AttributeError):
        s.hand_voided = True
    assert s.hand_voided is False


def test_completed_hand_is_recorded_and_not_a_void():
    bus, sessions, order = table()
    s = sessions["peer1"]
    assert s._end_hand(Session.HAND_COMPLETED, "showdown") is True
    assert s.hand_record.outcome == Session.HAND_COMPLETED
    assert s.hand_voided is False
    assert s.terminal_state is None
