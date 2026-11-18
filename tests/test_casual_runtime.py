"""Fake clocks drive real signed, cryptographic three-player sessions."""
from dataclasses import asdict

import pytest

from crypto_gate import require_crypto
from holdem.p2p.casual_runtime import runtime
from tests.test_casual_clock import SignedTable


def _table(*, start=True, offsets=(0, 0, 0), stacks=None):
    require_crypto()
    table = SignedTable(offsets)
    for s in table.sessions.values():
        s.configure_seats(table.order)
        s._adopt_deal_policy(s.DEAL_POLICY_BG)
        s._adopt_continuation_policy(s.CONTINUATION_SURVIVORS)
        s.state = "PLAYING"
        s.reliability_started()
    if start:
        for s in table.sessions.values():
            _begin(s, stacks=stacks)
        table.drain()
        assert all(s._own_hole_set for s in table.sessions.values())
    return table


def _begin(s, stacks=None):
    s.start_p2p_hand(hand_no=1, names=["P0", "P1", "P2"],
                     stacks=stacks or [1000] * 3, sb=5, bb=10, button=0)


def _actor(table):
    reference = table.sessions["P0"].replica
    return reference.actor, table.sessions[table.order[reference.actor]]


def _act(table, action="call"):
    seat, s = _actor(table)
    assert s.send_bet_action(action) == "applied"
    table.drain()
    return seat


def _synced(table, seats=(0, 1, 2)):
    assert len({table.sessions[table.order[seat]].replica.state_digest()
                for seat in seats}) == 1


def _settle(table, suppress=None):
    while table.sessions["P0"].replica.phase == "betting":
        _, s = _actor(table)
        assert s.send_bet_action("call") == "applied"
        table.drain(suppress=suppress)
    assert all(s.hand_result is not None for s in table.sessions.values())


def test_local_turn_folds_after_thirty_seconds_using_the_normal_signed_action():
    table = _table()
    seat, s = _actor(table)
    before = s.replica.next_seq
    assert s.replica.engine.legal(seat)["to_call"] > 0
    assert s.reliability_view()["remaining_seconds"] == 30
    table.advance(29.999)
    s.reliability_tick()
    assert s.replica.next_seq == before
    table.advance(.001)
    s.reliability_tick()
    table.drain()
    assert s.replica.next_seq == before + 1
    assert all(peer.replica.engine.players[seat].folded
               for peer in table.sessions.values())
    actions = [message for message in s._transport.sent if message["type"] == "bet_action"]
    assert actions[-1]["payload"]["action"] == "fold"
    _synced(table)


def test_local_turn_checks_when_no_call_is_due_and_board_unlocks_normally():
    table = _table()
    while True:
        seat, s = _actor(table)
        if s.replica.engine.legal(seat)["can_check"]:
            break
        _act(table)
    before = s.replica.next_seq
    live = [p.total_live for p in s.replica.engine.players]
    s.reliability_view()
    table.advance(30)
    s.reliability_tick()
    table.drain()
    assert s.replica.next_seq == before + 1
    assert all(peer.replica.engine.street == "flop" for peer in table.sessions.values())
    assert not s.replica.engine.players[seat].folded
    assert [message for message in s._transport.sent if message["type"] == "bet_action"][-1]["payload"]["action"] == "call"
    assert [p.total_live for p in s.replica.engine.players] == live
    _synced(table)


def test_postflop_free_check_is_applied_once_on_the_actors_own_clock():
    """Issue #11, retargeted: the approved casual auto-check, on a genuine
    POSTFLOP turn, through a real mental-poker hand.

    The check test above reaches a free check too, but it is the big
    blind's preflop option -- it asserts the street BECOMES the flop after
    the check. A postflop turn is a different state: the board has been
    unlocked by real deal shares and nobody has put in a chip on the
    street. The original issue asked for this through the session-layer
    timeout proposal, which no game applies and which a non-casual host is
    not supposed to act on at all; the approved auto-action lives here, in
    the casual runtime, so this is where it is proven.

    Whose clock acts is tested in both directions. A BYSTANDER's clock
    reaches thirty seconds first and nothing happens: no action, and no
    auto-action event from a game whose turn it is not. Then the ACTOR's
    clock reaches thirty and exactly one check is applied. Every game takes
    a turn's time from its own clock and acts only for its own seat --
    receivers never compare elapsed clocks.
    """
    table = _table()
    while table.sessions["P0"].replica.engine.street == "preflop":
        _act(table)

    # The state this test exists for, asserted rather than assumed. Each
    # of these failing means the test would otherwise pass for a reason
    # other than the one in its name.
    for cid, peer in table.sessions.items():
        assert peer._deal_driver is not None, f"{cid} has no real deal driver"
        assert type(peer.replica).__name__ == "ReplicaTable", cid
        assert peer.replica.engine.street == "flop", (
            f"{cid} is on {peer.replica.engine.street!r}, not a postflop street")
        assert len(peer.replica.engine.board) == 3, (
            f"{cid} has no unlocked flop: {peer.replica.engine.board}")
        assert peer.replica.phase == "betting", cid
        assert peer.hand_result is None and not peer.hand_voided, cid
    _synced(table)

    seat, s = _actor(table)
    legal = s.replica.engine.legal(seat)
    assert legal["can_check"] and legal["to_call"] == 0, (
        f"the postflop actor is not free to check: {legal}")

    before = {cid: peer.replica.next_seq for cid, peer in table.sessions.items()}
    live = [p.total_live for p in s.replica.engine.players]
    sent_before = len([m for m in s._transport.sent if m["type"] == "bet_action"])
    events = []

    class _Sink:
        def emit(self, payload):
            events.append(payload)

    s._log_sink = _Sink()
    actor_clock = table.clocks[seat]

    bystander_seat = next(i for i in range(3) if i != seat)
    bystander = table.sessions[table.order[bystander_seat]]
    bystander_events = []

    class _BystanderSink:
        def emit(self, payload):
            bystander_events.append(payload)

    bystander._log_sink = _BystanderSink()

    for peer in table.sessions.values():
        peer.reliability_view()             # starts each game's countdown
    table.advance(29.999)
    for peer in table.sessions.values():
        peer.reliability_tick()
    table.drain()
    assert s.replica.next_seq == before[s.local_conn_id],         "the game acted before the thirty seconds were up"

    # A bystander's clock gets there first. Its game must not act for a
    # seat that is not its own, nor report that it did.
    table.clocks[bystander_seat].advance(.001)
    bystander.reliability_tick()
    table.drain()
    for cid, peer in table.sessions.items():
        assert peer.replica.next_seq == before[cid], (
            f"{cid} applied an action on a BYSTANDER's expired clock")
    assert not [e for e in bystander_events
                if e.get("event") == "casual_auto_action"], (
        "a bystander's game reported acting for a seat that is not its own")

    actor_clock.advance(.001)
    for peer in table.sessions.values():    # every game ticks; one may act
        peer.reliability_tick()
    table.drain()

    # Exactly one action, and it was a check.
    for cid, peer in table.sessions.items():
        assert peer.replica.next_seq == before[cid] + 1, (
            f"{cid} applied {peer.replica.next_seq - before[cid]} actions, "
            f"not exactly one")
        assert not peer.replica.engine.players[seat].folded, (
            f"{cid} saw the actor FOLD when checking was free")
        assert [p.total_live for p in peer.replica.engine.players] == live, (
            f"{cid} saw chips move on a check")
    sent = [m for m in s._transport.sent if m["type"] == "bet_action"]
    assert len(sent) == sent_before + 1,         f"the actor sent {len(sent) - sent_before} bet actions, not one"
    # The engine's "call" command checks when nothing is owed; "check" is
    # the label the event carries.
    assert sent[-1]["payload"]["action"] == "call"
    assert sent[-1]["payload"].get("amount", 0) == 0
    auto = [e for e in events if e.get("event") == "casual_auto_action"]
    assert len(auto) == 1, f"expected one auto action, saw {auto}"
    assert auto[0]["action"] == "check" and auto[0]["seat"] == seat

    # It is no longer that seat's turn, and nothing fires twice.
    assert _actor(table)[0] != seat or s.replica.engine.street != "flop"
    for peer in table.sessions.values():
        peer.reliability_tick()
    table.drain()
    for cid, peer in table.sessions.items():
        assert peer.replica.next_seq == before[cid] + 1,             f"{cid} applied a second action on a repeated tick"
    _synced(table)


def test_timekeeper_drops_a_stalled_actor_at_45_without_receiver_elapsed_checks():
    table = _table()
    while _actor(table)[0] == 0:
        _act(table)
    lost, _ = _actor(table)
    host = table.sessions["P0"]
    survivor = table.sessions[table.order[next(seat for seat in [1, 2] if seat != lost)]]
    host.reliability_view()
    assert runtime(survivor).clock.tokens == ()  # Its local timer never started.
    table.advance(44.999)
    host.reliability_tick()
    assert not host.departed_seats
    assert host.reliability_view()["remaining_seconds"] == 1
    table.advance(.001)
    host.reliability_tick()
    table.drain()
    for s in [host, survivor]:
        assert lost in s.departed_seats
        assert not s.casual_transition_pending
        assert s.terminal_state is None
        assert sum(s._casual_continuation.carry["stacks"]) == 3000
    notices = [message for message in host._transport.sent
               if message["type"] == "casual_drop_observed"]
    assert notices[-1]["payload"]["scope"]["action_seq"] == host.replica.next_seq


def test_old_action_scope_and_a_forged_timekeeper_cannot_drop_a_player():
    table = _table()
    while _actor(table)[0] == 0:
        _act(table)
    host = table.sessions["P0"]
    token = runtime(host).waits(host)[0]
    lost = token.actor
    _act(table)
    assert not host.casual_drop(lost, "timeout", scope=asdict(token))
    assert not host.departed_seats
    # A real, correctly signed joiner still has no timekeeper authority.
    current = runtime(host).waits(host)[0]
    rogue = table.sessions["P2"]
    rogue._send_hostless(dict(type="casual_drop_observed", hand=current.hand,
                             seat=2, lost_seat=current.actor, reason="timeout",
                             scope=asdict(current)))
    table.drain()
    assert all(not s.departed_seats for s in table.sessions.values())


def test_missing_key_announcement_drops_after_30_and_refunds_undealt_blinds():
    table = _table(start=False)
    for name in ["P0", "P1"]:
        _begin(table.sessions[name])
    table.drain()
    host = table.sessions["P0"]
    view = host.reliability_view()
    assert view["kind"] == "automatic" and view["waiting_for"] == ["P2"]
    table.advance(29.999)
    host.reliability_tick()
    assert not host.departed_seats
    table.advance(.001)
    host.reliability_tick()
    # The absent player has not begun its hand, as on a hung application.
    table.drain(suppress=lambda to, _frm, _message: to == "P2")
    for name in ["P0", "P1"]:
        s = table.sessions[name]
        assert s.departed_seats == {2}
        assert s._casual_continuation.carry["stacks"] == [1000] * 3
        assert s.terminal_state is None


def test_next_hand_automatically_advances_all_three_copies_at_30():
    table = _table()
    _settle(table)
    stacks = table.sessions["P0"].replica.stacks
    for s in table.sessions.values():
        assert s.reliability_view()["kind"] == "next_hand"
    table.advance(29.999)
    for s in table.sessions.values():
        s.reliability_tick()
    assert all(s._hand_no == 1 for s in table.sessions.values())
    table.advance(.001)
    for s in table.sessions.values():
        s.reliability_tick()
    table.drain()
    assert all(s._hand_no == 2 and s._hand_stacks == stacks
               for s in table.sessions.values())
    _synced(table)


def test_missing_settlement_report_is_not_hidden_by_an_expired_next_hand_timer():
    table = _table()
    _settle(table, suppress=lambda _to, _frm, message:
            message["type"] == "hand_settled"
            and message["payload"]["seat"] == 2)
    host = table.sessions["P0"]
    assert host.next_p2p_hand() == "not_ready"
    assert host.reliability_view()["kind"] == "automatic"
    settled = host.replica.stacks
    table.advance(30)
    host.reliability_tick()
    table.drain()
    for name in ["P0", "P1"]:
        s = table.sessions[name]
        assert s.departed_seats == {2}
        assert s._casual_continuation.carry["stacks"] == settled
        assert s.terminal_state is None


def test_unrelated_betting_does_not_restart_missing_public_hole_share_clock():
    table = _table(start=False)
    for s in table.sessions.values():
        _begin(s)
    withheld = []
    # Suppress P1's public shares for P2's holes. P1 has its own real cards
    # and can legally act while the global public contribution is missing.
    from holdem.p2p.deal_map import hole_positions
    target = set(hole_positions(table.sessions["P0"].replica.button, [0, 1, 2])[2])
    def suppress(_to, _frm, message):
        payload = message["payload"]
        if (message["type"] == "deal_share" and payload["seat_from"] == 1
                and payload["position"] in target):
            withheld.append(message)
            return True
        return False
    table.drain(suppress=suppress)
    host = table.sessions["P0"]
    assert withheld and host._own_hole_set
    host.reliability_view()
    table.advance(20)
    actor, s = _actor(table)
    assert actor == 1 and s._own_hole_set
    assert s.send_bet_action("call") == "applied"
    table.drain(suppress=suppress)
    host.reliability_view()
    table.advance(10)
    host.reliability_tick()
    table.drain()
    assert 1 in host.departed_seats


def test_fixed_wall_offset_is_harmless_but_clock_jump_ends_host_table():
    table = _table()
    host = table.sessions["P0"]
    table.advance(20)
    host.reliability_tick()
    assert host.terminal_state is None
    table.clocks[0].advance_wall(30.001)
    host.reliability_tick()
    assert host.terminal_state == host.HOST_LOST
    assert "clock" in host.terminal_reason


def test_missing_departure_ack_has_its_own_clock_and_cannot_freeze_survivors():
    table = _table()
    host = table.sessions["P0"]
    assert host.casual_drop(2, "disconnect")
    table.drain(suppress=lambda _to, _frm, message:
                message["type"] == "casual_drop_ack"
                and message["payload"]["seat"] == 1)
    assert host.casual_transition_pending
    assert "P1" in host.reliability_view()["waiting_for"]
    assert runtime(host).waits(host)[0].phase == "drop_ack"
    table.advance(30)
    host.reliability_tick()
    table.drain()
    assert host.departed_seats == {1, 2}
    assert host.terminal_state == host.ENDED_NORMAL
    assert table.sessions["P1"].terminal_state is not None


def test_non_timekeeper_cannot_conflict_with_an_existing_membership_boundary():
    table = _table()
    host = table.sessions["P0"]
    assert host.casual_drop(2, "disconnect")
    held = []
    def suppress(_to, _frm, message):
        if message["type"] == "casual_drop_ack" and message["payload"]["seat"] == 1:
            held.append(message)
            return True
        return False
    table.drain(suppress=suppress)
    assert host.casual_transition_pending and host.terminal_state is None
    pending = host._casual_continuation.pending["notice"]
    # P1 owns this signature and claimed seat, but lacks timekeeper
    # authority. This must not turn an ordinary wait into a dispute.
    forged = dict(pending, type="casual_drop", seat=1, actions=777)
    table.sessions["P1"]._send_hostless(forged)
    table.drain()
    assert host.terminal_state is None
    assert host._casual_continuation.pending["notice"] == pending
    # Delivering the genuine signed ACK still completes the original
    # boundary; rejecting the forged one must not consume valid progress.
    assert held
    host.handle_message("P1", held[0])
    table.drain()
    assert not host.casual_transition_pending and host.departed_seats == {2}
    assert host.terminal_state is None


def test_joiner_clock_jump_is_dropped_even_when_signed_frames_still_fit_window():
    table = _table(offsets=(0, -10, 10))
    joiner = table.sessions["P1"]
    # All fixed offsets were harmless. The jump moves P1 to +21s, still
    # inside wire's +/-30s window at both other players.
    table.advance(1)
    joiner.reliability_tick()
    assert joiner.terminal_state is None
    table.clocks[1].advance_wall(31)
    joiner.reliability_tick()
    table.drain()
    assert "clock" in joiner.terminal_reason
    for name in ["P0", "P2"]:
        s = table.sessions[name]
        assert s.terminal_state is None
        assert s.departed_seats == {1}
        assert s._casual_continuation.last_notice["reason"] == "clock"


def test_just_busted_seat_automatically_becomes_a_spectator_at_30(monkeypatch):
    from tests.showdown_rig import rig_showdowns
    rig_showdowns(monkeypatch)(1, 0, 2)
    table = _table(stacks=[1000, 1000, 10])
    _, actor = _actor(table)
    assert actor.send_bet_action("raise", 20) == "applied"
    table.drain()
    _settle(table)
    busted = table.sessions["P2"]
    assert busted.replica.stacks[2] == 0 and busted._eliminated()
    assert not busted._p2p_spectator
    assert busted.reliability_view()["kind"] == "next_hand"
    table.advance(30)
    busted.reliability_tick()
    assert busted._p2p_spectator
    assert busted.reliability_view() is None
