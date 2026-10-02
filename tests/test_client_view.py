"""The client-facing boundary Godot will consume: per-player snapshots and
commands over a hostless session. The load-bearing property is the same
hidden-information invariant as contract.py -- a snapshot never carries
another seat's hole cards during play -- now proven end-to-end over real
sessions playing real hands. Every snapshot must be plain JSON.
"""
import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem import client_view
from holdem.p2p.session import Session
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport

try:
    importlib.import_module("holdem.p2p.elgamal")   # libsodium guard
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


def make_table(n, stacks=None):
    bus = InMemoryBus()
    order = [f"peer{i}" for i in range(n)]
    sessions = {}
    for i, cid in enumerate(order):
        s = Session(is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
                    transport=InMemoryTransport(bus, cid))
        s.local_conn_id = cid
        s.configure_seats(list(order))
        s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
        s._deal_master_secret = bytes([100 + i]) * 32   # deterministic deal
        bus.register(cid, s)
        sessions[cid] = s
    names = [f"P{i}" for i in range(n)]
    stacks = list(stacks) if stacks else [500] * n
    for cid in order:
        sessions[cid].start_p2p_hand(hand_no=1, names=names, stacks=stacks,
                                     sb=5, bb=10, button=0)
    bus.drain()
    return bus, sessions, order


def json_safe(d):
    """Round-trip through JSON; returns the reparsed object (raises if the
    snapshot is not serialisable -- the wire boundary requires it)."""
    return json.loads(json.dumps(d))


_RANKS = {"2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"}


def all_card_strings(obj):
    """Every card string anywhere in a nested structure. Card strings are
    rank+suit where rank may be two chars ('10s') -- the engine renders ten
    as '10', not 'T'."""
    out = []
    if isinstance(obj, str):
        if len(obj) in (2, 3) and obj[-1] in "cdhs" and obj[:-1] in _RANKS:
            out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out += all_card_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out += all_card_strings(v)
    return out


# --------------------------------------------------------------- snapshots

def test_snapshot_is_json_and_well_formed():
    bus, sessions, order = make_table(3)
    for cid in order:
        snap = client_view.snapshot(sessions[cid])
        snap = json_safe(snap)                         # must serialise
        assert snap["type"] == "snapshot"
        assert snap["phase"] == "betting"
        assert len(snap["seats"]) == 3
        assert snap["hand_num"] == 1
        assert snap["deal_progress"]["state"] == "in_hand"
        assert snap["events"][0]["event"] == "hand_started"
        assert snap["settlement"] is None


def test_local_hole_present_others_absent_during_play():
    """THE invariant: my snapshot shows my two cards and NO other seat's."""
    bus, sessions, order = make_table(3)
    for cid in order:
        snap = client_view.snapshot(sessions[cid])
        assert len(snap["you"]["hole"]) == 2           # I see my cards
        for sv in snap["seats"]:
            assert "hole" not in sv                     # nobody else's, anywhere
        # exactly my two cards appear in the whole snapshot
        assert sorted(all_card_strings(snap)) == sorted(snap["you"]["hole"])


def test_snapshots_across_seats_reveal_disjoint_holes():
    """Union the 'you.hole' each seat sees: 6 distinct cards, no overlap --
    proof the boundary partitions private information correctly."""
    bus, sessions, order = make_table(3)
    seen = []
    for cid in order:
        seen += client_view.snapshot(sessions[cid])["you"]["hole"]
    assert len(set(seen)) == len(seen) == 6


def test_legal_only_for_the_actor():
    bus, sessions, order = make_table(3)
    actor = sessions[order[0]].replica.actor
    for i, cid in enumerate(order):
        snap = client_view.snapshot(sessions[cid])
        if i == actor:
            assert "legal" in snap["you"]
            assert set(snap["you"]["legal"]) >= {"to_call", "can_check", "min_to"}
            assert snap["turn"]["state"] == "your_turn"
            assert snap["turn"]["decision"]["to_call"] == \
                snap["you"]["legal"]["to_call"]
        else:
            assert "legal" not in snap["you"]
            assert "decision" not in snap["turn"]


# --------------------------------------------------------------- commands

def test_command_drives_the_hand():
    bus, sessions, order = make_table(3)
    actor = sessions[order[0]].replica.actor
    res = client_view.apply_command(sessions[order[actor]], "check_call")
    assert res["ok"] and res["verdict"] == "applied"
    bus.drain()
    # the turn advanced for everyone
    for cid in order:
        assert sessions[cid].replica.actor != actor or \
            sessions[cid].replica.actor is None
        action = client_view.snapshot(sessions[cid])["events"][-1]
        assert action["event"] == "action"
        assert action["seat"] == actor


def test_command_from_wrong_seat_is_reported_not_applied():
    bus, sessions, order = make_table(3)
    actor = sessions[order[0]].replica.actor
    wrong = (actor + 1) % 3
    res = client_view.apply_command(sessions[order[wrong]], "fold")
    assert not res["ok"]
    assert res["verdict"] == "rejected"


def test_unknown_command_raises():
    bus, sessions, order = make_table(2)
    with pytest.raises(ValueError):
        client_view.apply_command(sessions[order[0]], "teleport")


# --------------------------------------------------------------- lifecycle

def test_settled_snapshot_tables_all_holes_at_showdown():
    """Play a full checkdown; the settled snapshot reveals every seat's
    cards (audit made them public) and carries the result."""
    bus, sessions, order = make_table(3)
    while sessions[order[0]].replica.phase == "betting":
        seat = sessions[order[0]].replica.actor
        client_view.apply_command(sessions[order[seat]], "check_call")
        bus.drain()
    snap = json_safe(client_view.snapshot(sessions[order[0]]))
    assert snap["phase"] == "settled"
    assert snap["result"] is not None
    assert snap["deal_progress"]["state"] == "settled"
    assert snap["turn"]["state"] == "hand_complete"
    assert snap["settlement"]["pots"]
    assert snap["settlement"]["showdown"]
    # collect every seat's tabled hole cards (mine from you.hole, others from
    # their seat view): 3 seats x 2 = 6 distinct cards
    holes = list(snap["you"]["hole"])
    for sv in snap["seats"]:
        if "hole" in sv:
            holes += sv["hole"]
    assert len(holes) == 6 and len(set(holes)) == 6
    assert len(snap["board"]) == 5                       # full board too


def test_showdown_never_reveals_a_seat_that_folded_earlier():
    """Gap C1 (POKER_RULES_PROFILE D-M1-1c): the audit opens every dealt
    seat, but only the seats that reached the showdown are tabled. A seat
    that folded on an earlier street must not appear in anyone's snapshot."""
    bus, sessions, order = make_table(3)
    folder = sessions[order[0]].replica.actor
    client_view.apply_command(sessions[order[folder]], "fold")
    bus.drain()
    _checkdown(bus, sessions, order)
    folded_hole = client_view.snapshot(sessions[order[folder]])["you"]["hole"]

    for i, cid in enumerate(order):
        snap = json_safe(client_view.snapshot(sessions[cid]))
        assert snap["phase"] == "settled" and snap["result"]["runs"]
        assert folder not in snap["result"]["shown"]
        tabled = {sv["seat"] for sv in snap["seats"] if "hole" in sv}
        assert tabled == {s for s in range(3) if s not in (i, folder)}
        if i != folder:
            assert not set(folded_hole) & set(all_card_strings(snap))


def test_foldout_settled_snapshot_reveals_nothing():
    bus, sessions, order = make_table(3)
    while sessions[order[0]].replica.phase == "betting":
        seat = sessions[order[0]].replica.actor
        client_view.apply_command(sessions[order[seat]], "fold")
        bus.drain()
    snap = client_view.snapshot(sessions[order[0]])
    assert snap["phase"] == "settled"
    for sv in snap["seats"]:
        assert "hole" not in sv                         # no showdown -> no reveal


def test_void_snapshot_reports_reason():
    bus, sessions, order = make_table(3)
    victim = sessions[order[0]]
    bad = {"type": "deal_share", "position": 0, "seat_from": 2,
           "hand": 1, "D_hex": "00" * 32, "dleq_hex": "11" * 64}
    victim.handle_message("peer2", bad)
    snap = client_view.snapshot(victim)
    assert snap["phase"] == "void"
    assert snap["voided"] is True
    assert "seat 2" in snap["void_reason"]


def test_lobby_snapshot_before_hand():
    bus = InMemoryBus()
    order = ["peer0", "peer1"]
    s = Session(is_host=True, nickname="P0", avatar_b64="",
                transport=InMemoryTransport(bus, "peer0"))
    s.local_conn_id = "peer0"
    s.configure_seats(list(order))
    s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
    snap = client_view.snapshot(s)
    assert snap["phase"] == "lobby"
    assert snap["you"]["seat"] == 0
    assert len(snap["seats"]) == 2


# ------------------------------------------------ continuous play (client)

def _checkdown(bus, sessions, order):
    from holdem.p2p.replica_table import PHASE_BETTING
    while sessions[order[0]].replica.phase == PHASE_BETTING:
        seat = sessions[order[0]].replica.actor
        assert client_view.apply_command(
            sessions[order[seat]], "check_call")["verdict"] == "applied"
        bus.drain()


def test_next_hand_command_advances_the_table():
    """The next_hand command drives the continuous session: after a hand
    settles, every peer's client issues it and hand 2 begins, reported as
    'started'. Mid-hand it reports 'not_ready'."""
    bus, sessions, order = make_table(3)
    # mid-hand: not ready
    res = client_view.apply_command(sessions[order[0]], "next_hand")
    assert res["command"] == "next_hand" and res["verdict"] == "not_ready"
    assert res["ok"] is False

    _checkdown(bus, sessions, order)
    for cid in order:
        assert sessions[cid].hand_result is not None
    results = [client_view.apply_command(sessions[cid], "next_hand")
               for cid in order]
    bus.drain()
    assert all(r["verdict"] == "started" and r["ok"] for r in results)
    for cid in order:
        assert sessions[cid]._hand_no == 2
        snap = json_safe(client_view.snapshot(sessions[cid]))
        assert snap["phase"] in ("dealing", "betting")
        assert snap["session_over"] is False


def test_snapshot_reports_session_over_and_winner():
    """When the match ends, the snapshot carries session_over + the winning
    seat so the client can show 'game over' and stop offering next_hand."""
    from holdem.p2p.replica_table import PHASE_BETTING
    bus, sessions, order = make_table(2, stacks=[500, 500])
    # Heads-up, all-in every hand until one side owns all 1000 chips.
    for _ in range(60):
        while sessions[order[0]].replica.phase == PHASE_BETTING:
            seat = sessions[order[0]].replica.actor
            r = sessions[order[seat]].replica
            lg = r.engine.legal(r.actor)
            act = "raise" if lg["can_raise"] else "call"
            amt = lg["max_to"] if act == "raise" else 0
            client_view.apply_command(
                sessions[order[seat]], "raise_to", {"amount": amt}) \
                if act == "raise" else \
                client_view.apply_command(sessions[order[seat]], "check_call")
            bus.drain()
        verdicts = [client_view.apply_command(sessions[cid], "next_hand")
                    for cid in order]
        bus.drain()
        if any(v["verdict"] == "session_over" for v in verdicts):
            break
    else:
        pytest.fail("heads-up match did not resolve")

    # Every peer that still has a replica agrees the match is over and names
    # the same winner in its snapshot.
    winners = set()
    for cid in order:
        snap = json_safe(client_view.snapshot(sessions[cid]))
        if snap.get("phase") == "lobby":
            continue                          # busted peer with no replica
        assert snap["session_over"] is True
        winners.add(snap["session_winner"])
    assert len(winners) == 1 and winners.pop() is not None


def test_eliminated_snapshot_receives_terminal_match_state():
    """A busted client's retained hand view can still render the winner."""
    _, sessions, order = make_table(3)
    session = sessions[order[0]]
    session._p2p_spectator = True
    session._session_over = True
    session._session_winner = 2
    session._final_stacks = [0, 0, 1500]
    session.terminate(Session.ENDED_NORMAL, "match complete; winner seat 2")

    snap = json_safe(client_view.snapshot(session))
    assert snap["eliminated"] is True
    assert snap["session_over"] is True
    assert snap["session_winner"] == 2
    assert snap["final_stacks"] == [0, 0, 1500]
    assert snap["terminal"]["last_settled_stacks"] == [0, 0, 1500]
    assert snap["turn"]["state"] == "match_complete"
    assert snap["turn"]["headline"] == "P2 won the match"


def _play_hand(bus, sessions, order, alive, shovers):
    """Play one hand through client commands: seats in ``shovers`` move all
    in whenever they may raise, everyone else checks or calls."""
    from holdem.p2p.replica_table import PHASE_BETTING
    ref = sessions[order[alive[0]]]
    while ref.replica.phase == PHASE_BETTING:
        seat = ref.replica.actor
        legal = ref.replica.engine.legal(seat)
        if seat in shovers and legal["can_raise"]:
            res = client_view.apply_command(sessions[order[seat]], "raise_to",
                                            {"amount": legal["max_to"]})
        else:
            res = client_view.apply_command(sessions[order[seat]], "check_call")
        assert res["verdict"] == "applied", res
        bus.drain()


def test_a_seat_busted_before_the_end_reports_the_match_final_stacks():
    """The retained-replica case, played for real. A busted seat keeps the
    hand that eliminated it and drops every later hand, so when the match
    ends its replica is stale; session_end brings it the final stacks, and
    those are the last settlement its terminal reports."""
    bus, sessions, order = make_table(3, stacks=[1000, 20, 1000])
    # The short seat shoves and the others only call, so seat 1 is the one
    # to bust -- in whichever hand the cards decide.
    for _ in range(40):
        _play_hand(bus, sessions, order, [0, 1, 2], shovers={1})
        verdicts = [client_view.apply_command(sessions[cid], "next_hand")
                    for cid in order]
        bus.drain()
        if verdicts[1]["verdict"] == "eliminated":
            break
    else:
        pytest.fail("the short seat never busted")
    assert [v["verdict"] for v in verdicts] == ["started", "eliminated",
                                                "started"]
    spectator = sessions[order[1]]
    retained = list(spectator.replica.stacks)

    # Heads-up shoves until one survivor holds every chip (a chop replays).
    survivors = [sessions[order[0]], sessions[order[2]]]
    for _ in range(40):
        _play_hand(bus, sessions, order, [0, 2], shovers={0, 2})
        final = list(survivors[0].replica.stacks)
        verdicts = [client_view.apply_command(s, "next_hand")
                    for s in survivors]
        bus.drain()
        if verdicts[0]["verdict"] == "session_over":
            break
    else:
        pytest.fail("the heads-up match never ended")

    assert retained != final                      # the table moved on
    snap = json_safe(client_view.snapshot(spectator))
    assert snap["eliminated"] is True and snap["session_over"] is True
    assert snap["final_stacks"] == final
    assert snap["terminal"]["state"] == Session.ENDED_NORMAL
    assert snap["terminal"]["last_settled_stacks"] == final


# ------------------------------------------------------ terminal contract

def _lobby_session():
    s = Session(is_host=True, nickname="P0", avatar_b64="",
                transport=InMemoryTransport(InMemoryBus(), "peer0"))
    s.local_conn_id = "peer0"
    s.configure_seats(["peer0", "peer1"])
    return s


def test_a_live_session_has_no_terminal_in_either_snapshot_shape():
    _, sessions, order = make_table(2)
    assert json_safe(client_view.snapshot(sessions[order[0]]))["terminal"] is None
    assert json_safe(client_view.snapshot(_lobby_session()))["terminal"] is None


def test_a_table_closed_mid_hand_reports_the_last_settlement_not_the_pot():
    """The in-flight pot is discarded: the stacks to show are the ones the
    previous hand settled, not the live hand's partly-bet ones."""
    bus, sessions, order = make_table(2)
    _checkdown(bus, sessions, order)
    settled = sessions[order[0]].replica.stacks
    for cid in order:
        client_view.apply_command(sessions[cid], "next_hand")
    bus.drain()
    actor = sessions[order[0]].replica.actor
    legal = sessions[order[actor]].replica.engine.legal(actor)
    client_view.apply_command(sessions[order[actor]], "raise_to",
                              {"amount": legal["min_to"]})
    bus.drain()
    me = sessions[order[sessions[order[0]].replica.actor]]
    assert "legal" in client_view.snapshot(me)["you"]          # my turn

    me.terminate(Session.ABORTED_PROTOCOL, "seat 1 disconnected")
    snap = json_safe(client_view.snapshot(me))

    assert snap["terminal"] == {"state": Session.ABORTED_PROTOCOL,
                                "reason": "seat 1 disconnected",
                                "last_settled_stacks": settled}
    assert sum(sv["stack"] for sv in snap["seats"]) < sum(settled)
    assert snap["turn"]["state"] == "table_closed"
    assert snap["turn"]["headline"] == "seat 1 disconnected"
    assert "decision" not in snap["turn"]
    assert "legal" not in snap["you"]


def test_a_table_closed_after_a_settled_hand_reports_that_settlement():
    bus, sessions, order = make_table(2)
    _checkdown(bus, sessions, order)
    me = sessions[order[0]]
    assert me.replica.stacks != [500, 500]       # the hand moved chips
    me.terminate(Session.HOST_LOST, "host connection dropped during play")
    snap = json_safe(client_view.snapshot(me))
    assert snap["terminal"]["last_settled_stacks"] == me.replica.stacks
    assert snap["turn"]["state"] == "table_closed"


def test_a_table_closed_on_a_voided_hand_reports_its_carry_in():
    bus, sessions, order = make_table(3)
    victim = sessions[order[0]]
    victim.handle_message("peer2", {
        "type": "deal_share", "position": 0, "seat_from": 2, "hand": 1,
        "D_hex": "00" * 32, "dleq_hex": "11" * 64})
    assert victim.hand_voided
    victim.terminate(Session.ABORTED_PROTOCOL, "deal failed")
    snap = json_safe(client_view.snapshot(victim))
    assert snap["terminal"]["last_settled_stacks"] == [500, 500, 500]


def test_a_lobby_that_ends_closes_the_table_with_no_stacks():
    s = _lobby_session()
    s.terminate(Session.HOST_LOST, "host lost in lobby")
    snap = json_safe(client_view.snapshot(s))
    assert snap["phase"] == "lobby"
    assert snap["terminal"] == {"state": Session.HOST_LOST,
                                "reason": "host lost in lobby",
                                "last_settled_stacks": None}
    assert snap["turn"]["state"] == "table_closed"
    assert snap["turn"]["headline"] == "host lost in lobby"


def test_a_reason_quoting_the_host_is_cut_before_it_reaches_the_client():
    """POLICY_REFUSED quotes the deal policy the host declared, whatever it
    was. Session keeps that reason whole; the snapshot -- the headline the
    client shows and terminal.reason -- carries 512 characters of it."""
    s = Session(is_host=False, nickname="P1", avatar_b64="",
                transport=InMemoryTransport(InMemoryBus(), "peer1"))
    s.local_conn_id = "peer1"
    s._host_conn_id = "peer0"
    declared = ("Your opponent forfeited. Claim your winnings at "
                "http://example.invalid/claim " + "X" * 200_000)
    s.handle_message("peer0", {"type": "game_start", "payload": {
        "seat_order": ["peer0", "peer1"],
        "table_settings": {Session.DEAL_POLICY_SETTING: declared}}})
    assert s.terminal_state == Session.POLICY_REFUSED
    assert len(s.terminal_reason) > 200_000

    snap = json_safe(client_view.snapshot(s))
    assert snap["turn"]["state"] == "table_closed"
    assert snap["turn"]["headline"] == s.terminal_reason[:512]
    assert snap["terminal"]["reason"] == s.terminal_reason[:512]
    assert len(json.dumps(snap)) < 4096


def test_a_finished_match_is_reported_but_is_not_a_closed_table():
    _, sessions, order = make_table(2)
    me = sessions[order[0]]
    me.terminate(Session.ENDED_NORMAL, "match complete; winner seat 0")
    snap = json_safe(client_view.snapshot(me))
    assert snap["terminal"]["state"] == Session.ENDED_NORMAL
    assert snap["turn"]["state"] != "table_closed"


def test_start_game_command_invokes_the_controller_callable():
    """start_game routes to the controller's callable and echoes its verdict.

    The adapter must not synthesise table settings of its own: proposed
    configuration belongs to the controller that assembled the table, and
    accepted protocol state belongs to the Session.
    """
    _, sessions, order = make_table(3)
    calls = []

    res = client_view.apply_command(
        sessions[order[0]], "start_game",
        start_table=lambda: (calls.append(1), "started")[1])

    assert calls == [1], "controller callable was not invoked"
    assert res["verdict"] == "started"
    assert res["ok"] is True


def test_start_game_verdicts_other_than_started_are_not_ok():
    """ok is true only for 'started'. A refused or duplicate start must not
    read as success, or the client will leave the lobby on a table that
    never began."""
    _, sessions, order = make_table(3)
    for verdict in ("already_started", "refused", "hand_failed"):
        res = client_view.apply_command(
            sessions[order[0]], "start_game",
            start_table=lambda v=verdict: v)
        assert res["verdict"] == verdict
        assert res["ok"] is False, f"{verdict} wrongly reported ok"


def test_start_game_without_a_controller_is_an_error_not_a_silent_noop():
    """A sidecar wired without a start callable must say so. Returning a
    polite failure verdict here would look identical to a table that
    declined to start, and the client could not tell a misconfigured
    sidecar from a refused one."""
    _, sessions, order = make_table(3)
    with pytest.raises(ValueError):
        client_view.apply_command(sessions[order[0]], "start_game")


if __name__ == "__main__":
    passed = total = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            total += 1
            try:
                fn()
                passed += 1
                print(f"  {name}: ok")
            except Exception as exc:
                print(f"  {name}: FAIL - {type(exc).__name__}: {exc}")
    print(f"{passed}/{total} passed")
