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
        s._seat_order = list(order)
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
        assert snap["verification"]["state"] == "audit_pending"
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
    actor = sessions[order[0]]._replica.actor
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
    actor = sessions[order[0]]._replica.actor
    res = client_view.apply_command(sessions[order[actor]], "check_call")
    assert res["ok"] and res["verdict"] == "applied"
    bus.drain()
    # the turn advanced for everyone
    for cid in order:
        assert sessions[cid]._replica.actor != actor or \
            sessions[cid]._replica.actor is None
        action = client_view.snapshot(sessions[cid])["events"][-1]
        assert action["event"] == "action"
        assert action["seat"] == actor


def test_command_from_wrong_seat_is_reported_not_applied():
    bus, sessions, order = make_table(3)
    actor = sessions[order[0]]._replica.actor
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
    while sessions[order[0]]._replica.phase == "betting":
        seat = sessions[order[0]]._replica.actor
        client_view.apply_command(sessions[order[seat]], "check_call")
        bus.drain()
    snap = json_safe(client_view.snapshot(sessions[order[0]]))
    assert snap["phase"] == "settled"
    assert snap["result"] is not None
    assert snap["verification"]["state"] == "verified"
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


def test_foldout_settled_snapshot_reveals_nothing():
    bus, sessions, order = make_table(3)
    while sessions[order[0]]._replica.phase == "betting":
        seat = sessions[order[0]]._replica.actor
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
    s._seat_order = list(order)
    snap = client_view.snapshot(s)
    assert snap["phase"] == "lobby"
    assert snap["you"]["seat"] == 0
    assert len(snap["seats"]) == 2


# ------------------------------------------------ continuous play (client)

def _checkdown(bus, sessions, order):
    from holdem.p2p.replica_table import PHASE_BETTING
    while sessions[order[0]]._replica.phase == PHASE_BETTING:
        seat = sessions[order[0]]._replica.actor
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
        while sessions[order[0]]._replica.phase == PHASE_BETTING:
            seat = sessions[order[0]]._replica.actor
            r = sessions[order[seat]]._replica
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

    snap = json_safe(client_view.snapshot(session))
    assert snap["eliminated"] is True
    assert snap["session_over"] is True
    assert snap["session_winner"] == 2
    assert snap["final_stacks"] == [0, 0, 1500]
    assert snap["turn"]["state"] == "match_complete"
    assert snap["turn"]["headline"] == "P2 won the match"


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
