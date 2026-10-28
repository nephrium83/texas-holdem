"""Casual survivors settle locally and agree before a fresh key ceremony."""
import pytest

from holdem import client_view
from holdem.p2p.session import Session
from tests.test_peer_loss import table, act, settle_by_checkdown, play, folds
from tests.showdown_rig import rig_showdowns


def casual_table(n=3, **kwargs):
    bus, sessions, order = table(n, **kwargs)
    for session in sessions.values():
        session.enable_casual_reliability()
    return bus, sessions, order


def finish(bus, sessions, live):
    ref = sessions[live[0]]
    for _ in range(100):
        if ref.hand_result is not None:
            break
        actor = ref.replica.actor
        assert sessions[f"peer{actor}"].send_bet_action("call") == "applied"
        bus.drain()
    assert all(sessions[cid].hand_result is not None for cid in live)


def disconnect(bus, sessions, seat):
    bus.unregister(f"peer{seat}")
    sessions["peer0"].handle_disconnect(f"peer{seat}")
    bus.drain()


@pytest.mark.parametrize("lost", [1, 2])
def test_survivors_keep_fixed_seats_balances_and_play_again(lost):
    bus, sessions, order = casual_table()
    act(bus, sessions, order)
    keys = dict(sessions["peer0"]._seat_keys)
    disconnect(bus, sessions, lost)
    live = [cid for cid in order if cid != f"peer{lost}"]
    settled = sessions[live[0]].last_settled_stacks
    assert sum(settled) == 1500
    for cid in live:
        s = sessions[cid]
        assert s.terminal_state is None
        assert s.last_settled_stacks == settled
        snap = client_view.snapshot(s)
        assert [seat["stack"] for seat in snap["seats"]] == settled
        assert all(seat["bet"] == 0 for seat in snap["seats"])
        assert snap["pot"] == 0
        assert s.seat_order == order
        assert s._seat_keys == keys
        assert s.departed_seats == {lost}
        assert not s.casual_transition_pending
        assert s.next_p2p_hand() == "started"
    bus.drain()
    for cid in live:
        s = sessions[cid]
        assert s._hand_no == 3
        assert s.replica.seats_dealt == [seat for seat in range(3) if seat != lost]
        assert s.replica.stacks[lost] == settled[lost]
        assert s._drop_stacks is None
    finish(bus, sessions, live)
    assert sessions[live[0]].last_settled_stacks == sessions[live[1]].last_settled_stacks
    assert sum(sessions[live[0]].last_settled_stacks) == 1500


@pytest.mark.parametrize("first", [0, 1])
def test_ahead_copy_cancels_and_every_survivor_reserves_the_same_fresh_hand(first):
    bus, sessions, order = casual_table()
    settle_by_checkdown(bus, sessions, order)
    settled = sessions["peer0"].last_settled_stacks
    assert sessions["peer1"].next_p2p_hand() == "started"
    bus.drain()
    disconnect(bus, sessions, 2)
    for cid in (order[first], order[1 - first]):
        s = sessions[cid]
        assert s.terminal_state is None
        assert s.last_settled_stacks == settled
        assert s.next_p2p_hand() == "started"
        assert s._hand_no == 3
        bus.drain()  # The other survivor has not started the fresh hand yet.
    bus.drain()
    finish(bus, sessions, order[:2])


def test_undealt_drop_restores_blinds_then_starts_without_the_leaver():
    bus, sessions, order = casual_table()
    settle_by_checkdown(bus, sessions, order)
    settled = sessions["peer0"].last_settled_stacks
    for cid in order[:2]:
        assert sessions[cid].next_p2p_hand() == "started"
    bus.drain()
    assert not sessions["peer0"]._deal_driver.deal.is_shuffle_complete()
    disconnect(bus, sessions, 2)
    for cid in order[:2]:
        assert sessions[cid].last_settled_stacks == settled
        assert sessions[cid].next_p2p_hand() == "started"
        assert sessions[cid]._hand_no == 4
    bus.drain()
    finish(bus, sessions, order[:2])


def test_two_player_drop_completes_match_without_awarding_uncommitted_chips():
    bus, sessions, order = casual_table(2)
    act(bus, sessions, order)
    disconnect(bus, sessions, 1)
    host = sessions["peer0"]
    assert host.terminal_state == Session.ENDED_NORMAL
    assert host._session_winner == 0
    assert host.last_settled_stacks == [510, 490]
    snap = client_view.snapshot(host)
    assert [seat["stack"] for seat in snap["seats"]] == [510, 490]
    assert all(seat["bet"] == 0 for seat in snap["seats"])
    assert snap["pot"] == 0


def test_all_in_host_is_still_timekeeper():
    bus, sessions, order = casual_table()
    for s in sessions.values():
        s.replica.engine.players[0].stack = 0
        s.replica.engine.players[0].all_in = True
        assert s.casual_timekeeper_seat() == 0


def test_crossed_action_ends_both_survivors_as_disputed():
    bus, sessions, order = casual_table()
    host, other = sessions["peer0"], sessions["peer1"]
    act(bus, sessions, order)
    act(bus, sessions, order, "fold")
    act(bus, sessions, order)
    assert host.send_bet_action("raise", 20) == "applied"
    bus.drain()
    assert other.send_bet_action("fold") == "applied"
    disconnect(bus, sessions, 2)
    assert host.terminal_state == other.terminal_state == Session.ABORTED_PROTOCOL
    assert "disputed" in host.terminal_reason
    assert "disputed" in other.terminal_reason
    assert host.last_settled_stacks != other.last_settled_stacks


def test_next_deal_waits_for_each_survivors_signed_result():
    bus, sessions, order = casual_table()
    other = sessions["peer1"]
    sent = other._transport.broadcast
    held = []
    other._transport.broadcast = lambda msg: (held.append(dict(msg))
        if msg.get("type") == "casual_drop_ack" else sent(msg))
    disconnect(bus, sessions, 2)
    host = sessions["peer0"]
    assert host.casual_transition_pending
    assert host.next_p2p_hand() == "not_ready"
    snap = client_view.snapshot(host)
    assert [seat["stack"] for seat in snap["seats"]] == host.last_settled_stacks
    assert all(seat["bet"] == 0 for seat in snap["seats"])
    assert snap["pot"] == 0
    assert host._casual_continuation.missing_ack_seats() == [1]
    for msg in held:
        sent(msg)
    bus.drain()
    assert not host.casual_transition_pending
    assert host.next_p2p_hand() == "started"


def test_a_lie_in_survivor_result_ends_every_copy():
    bus, sessions, order = casual_table()
    other = sessions["peer1"]
    sent = other._transport.broadcast

    def corrupt(msg):
        msg = dict(msg)
        if msg.get("type") == "casual_drop_ack":
            msg["stacks"] = list(msg["stacks"])
            msg["stacks"][0] += 1
            msg["stacks"][1] -= 1
        sent(msg)

    other._transport.broadcast = corrupt
    disconnect(bus, sessions, 2)
    assert sessions["peer0"].terminal_state == Session.ABORTED_PROTOCOL
    assert sessions["peer1"].terminal_state == Session.ABORTED_PROTOCOL


def test_a_late_departed_seat_cannot_act_or_rejoin():
    bus, sessions, order = casual_table()
    disconnect(bus, sessions, 2)
    host = sessions["peer0"]
    before = host.last_settled_stacks
    host.handle_message("peer2", {"type": "hand_void", "seat": 2,
                                  "hand": 1, "reason": "late rollback"})
    assert host.terminal_state is None
    assert host.last_settled_stacks == before
    assert host.departed_seats == {2}


@pytest.mark.parametrize("extra_hands", [0, 2])
def test_busted_host_relays_the_active_timekeepers_boundary_and_follows(
        monkeypatch, extra_hands):
    rig_showdowns(monkeypatch)(1, 0, 2, 3)
    bus, sessions, order = casual_table(4, stacks=[20, 500, 500, 100])
    play(bus, sessions, order, folds(2, 3))
    assert sessions["peer0"].replica.stacks[0] == 0
    assert [sessions[c].next_p2p_hand() for c in order] == [
        "eliminated", "started", "started", "started"]
    bus.drain()
    for _ in range(extra_hands):
        finish(bus, sessions, order[1:])
        for cid in order[1:]:
            assert sessions[cid].next_p2p_hand() == "started"
        bus.drain()
    assert sessions["peer0"].casual_timekeeper_seat() == 1
    disconnect(bus, sessions, 3)
    expected = sessions["peer1"].last_settled_stacks
    for cid in order[:3]:
        assert sessions[cid].terminal_state is None
        assert sessions[cid].departed_seats == {3}
        assert sessions[cid].last_settled_stacks == expected
    for cid in order[1:3]:
        assert sessions[cid].next_p2p_hand() == "started"
    bus.drain()
    finish(bus, sessions, order[1:3])
    assert sessions["peer0"].last_settled_stacks == sessions["peer1"].last_settled_stacks


def test_second_disconnect_during_ack_wait_is_serialized_without_waiting_on_it():
    bus, sessions, order = casual_table(4)
    second = sessions["peer2"]
    sent = second._transport.broadcast
    second._transport.broadcast = lambda msg: (
        None if msg.get("type") == "casual_drop_ack" else sent(msg))
    disconnect(bus, sessions, 3)
    host = sessions["peer0"]
    assert host.casual_transition_pending
    first = host.last_settled_stacks
    disconnect(bus, sessions, 2)
    for cid in order[:2]:
        s = sessions[cid]
        assert s.terminal_state is None
        assert s.departed_seats == {2, 3}
        assert not s.casual_transition_pending
        assert s.last_settled_stacks == first
        assert s.next_p2p_hand() == "started"
        assert s._hand_no == 4
    bus.drain()
    finish(bus, sessions, order[:2])


def test_only_host_can_report_a_socket_drop():
    bus, sessions, order = casual_table()
    assert not sessions["peer1"].casual_drop(2, "disconnect")
    sessions["peer1"]._send_hostless({"type": "casual_drop_observed",
        "hand": 1, "lost_seat": 2, "reason": "disconnect", "scope": None})
    bus.drain()
    assert all(not s.departed_seats and not s.casual_transition_pending
               for s in sessions.values())


def test_host_reported_signed_clock_skew_does_not_need_peer_self_report():
    bus, sessions, order = casual_table()
    bus.unregister("peer2")
    sessions["peer0"].handle_disconnect("peer2", reason="clock")
    bus.drain()
    assert sessions["peer0"].departed_seats == sessions["peer1"].departed_seats == {2}
    assert sessions["peer0"]._casual_continuation.last_notice["reason"] == "clock"


def test_missing_prevention_proof_ends_in_dispute_without_dealing_again():
    bus, sessions, order = casual_table()
    sessions["peer0"]._deal_driver.deal._proofs_verified -= 1
    disconnect(bus, sessions, 2)
    assert sessions["peer0"].terminal_state == Session.ABORTED_PROTOCOL
    assert sessions["peer1"].terminal_state == Session.ABORTED_PROTOCOL
    assert sessions["peer0"].next_p2p_hand() == "session_over"


def test_missing_ack_timeout_cancels_an_ahead_survivors_reserved_deal():
    bus, sessions, order = casual_table(4)
    for s in sessions.values():
        s.reliability_started()
    stalled = sessions["peer2"]
    send = stalled._transport.broadcast

    def omit_host_ack(msg):
        if msg.get("type") == "casual_drop_ack":
            # Model selective delivery: the ahead survivor received the
            # signed report while the timekeeper still waits for it.
            bus.enqueue("peer2", "peer1", dict(msg))
        else:
            send(msg)

    stalled._transport.broadcast = omit_host_ack
    disconnect(bus, sessions, 3)
    host, ahead = sessions["peer0"], sessions["peer1"]
    expected = host.last_settled_stacks
    assert host.casual_transition_pending
    assert ahead.next_p2p_hand() == "started"
    bus.drain()
    assert ahead._hand_no == 3
    assert not ahead._deal_driver.deal.is_shuffle_complete()
    host.reliability_tick()
    host._clock.advance(30.01)
    host.reliability_tick()
    bus.drain()
    for cid in order[:2]:
        s = sessions[cid]
        assert s.terminal_state is None
        assert s.departed_seats == {2, 3}
        assert s.last_settled_stacks == expected
        assert s.next_p2p_hand() == "started"
        assert s._hand_no == 4
    assert stalled.terminal_state == Session.PEER_LOST
    assert "timekeeper" in stalled.terminal_reason
    bus.drain()
    finish(bus, sessions, order[:2])
