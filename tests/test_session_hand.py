"""The capstone of L5: real Session instances play COMPLETE hostless hands
over the in-memory bus -- trustless mental-poker deal + replica-engine
betting + automatic street reveals + post-hand audit + settlement -- with
no host anywhere. Every piece built in steps 1-3 runs together here.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.session import Session
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.replica_table import (
    PHASE_BETTING, PHASE_SETTLED)

import importlib
try:
    importlib.import_module("holdem.p2p.elgamal")   # libsodium guard
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


def make_table(n, stacks=None, sb=5, bb=10, hand=1, button=0):
    bus = InMemoryBus()
    order = [f"peer{i}" for i in range(n)]
    sessions = {}
    for i, cid in enumerate(order):
        s = Session(is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
                    transport=InMemoryTransport(bus, cid))
        s.local_conn_id = cid
        s._seat_order = list(order)
        bus.register(cid, s)
        sessions[cid] = s
    names = [f"P{i}" for i in range(n)]
    stacks = list(stacks) if stacks else [500] * n
    for cid in order:
        sessions[cid].start_p2p_hand(hand_no=hand, names=names,
                                     stacks=stacks, sb=sb, bb=bb,
                                     button=button)
    bus.drain()
    return bus, sessions, order


def replicas(sessions, order):
    return [sessions[c]._replica for c in order]


def assert_synced(sessions, order):
    digs = [r.state_digest() for r in replicas(sessions, order)]
    assert len(set(digs)) == 1, f"replicas diverged: {digs}"


def act(bus, sessions, order, action, amount=0):
    """The current actor's SESSION acts; everyone else hears it on the bus."""
    seat = sessions[order[0]]._replica.actor
    assert seat is not None
    verdict = sessions[order[seat]].send_bet_action(action, amount)
    assert verdict == "applied", f"seat {seat} {action}: {verdict}"
    bus.drain()
    assert_synced(sessions, order)


def test_hand_starts_synced_with_private_holes():
    bus, sessions, order = make_table(3)
    assert_synced(sessions, order)
    for cid in order:
        s = sessions[cid]
        assert s._replica.phase == PHASE_BETTING
        assert all(c is not None for c in s.deal_hole_cards)
        assert s._own_hole_set
    # privacy across sessions: 6 distinct cards, none shared
    every = []
    for cid in order:
        every.extend((c.v, c.s) for c in sessions[cid].deal_hole_cards)
    assert len(set(every)) == len(every)


def test_full_checkdown_hand_settles_identically():
    """Calls/checks to showdown: streets reveal + advance automatically,
    the audit runs, settle is identical everywhere, chips conserved."""
    bus, sessions, order = make_table(3)
    while sessions[order[0]]._replica.phase == PHASE_BETTING:
        act(bus, sessions, order, "call")
    # the pump carried everything else: streets, audit, settlement
    results = [sessions[c].hand_result for c in order]
    assert all(r is not None for r in results)
    assert all(r == results[0] for r in results)
    for cid in order:
        s = sessions[cid]
        assert s._replica.phase == PHASE_SETTLED
        assert s.deal_done() and not s.hand_voided
        assert len(s._replica.engine.board) == 5
        assert sum(p.stack for p in s._replica.engine.players) == 1500
    assert len(results[0]["winners"]) >= 1


def test_fold_out_settles_without_board():
    bus, sessions, order = make_table(3)
    while sessions[order[0]]._replica.phase == PHASE_BETTING:
        act(bus, sessions, order, "fold")
    results = [sessions[c].hand_result for c in order]
    assert all(r is not None and r == results[0] for r in results)
    for cid in order:
        assert sessions[cid]._replica.engine.board == []       # no reveal needed
        assert sessions[cid].deal_done()                       # audit still ran
        assert sum(p.stack for p in sessions[cid]._replica.engine.players) == 1500


def test_all_in_lockup_cascades_to_settlement():
    """All-in preflop with three stack sizes: one drain cascades every
    street reveal, the audit, and a layered side-pot settlement."""
    bus, sessions, order = make_table(3, stacks=[200, 60, 120])
    while sessions[order[0]]._replica.phase == PHASE_BETTING:
        r = sessions[order[0]]._replica
        lg = r.engine.legal(r.actor)
        if lg["can_raise"]:
            act(bus, sessions, order, "raise", lg["max_to"])
        else:
            act(bus, sessions, order, "call")
    results = [sessions[c].hand_result for c in order]
    assert all(r is not None and r == results[0] for r in results)
    assert len(results[0]["pots"]) >= 2                        # side pots layered
    for cid in order:
        assert len(sessions[cid]._replica.engine.board) == 5   # ran out fully
        assert sum(p.stack for p in sessions[cid]._replica.engine.players) == 380


def test_out_of_turn_action_rejected_everywhere():
    bus, sessions, order = make_table(3)
    actor = sessions[order[0]]._replica.actor
    wrong = (actor + 1) % 3
    d0 = sessions[order[0]]._replica.state_digest()
    verdict = sessions[order[wrong]].send_bet_action("call")
    assert verdict == "rejected"                # local replica refuses; nothing sent
    bus.drain()
    assert sessions[order[0]]._replica.state_digest() == d0
    assert_synced(sessions, order)


def test_desync_is_detected_and_voids():
    """A corrupted replica (simulated bug/tamper) is caught by the digest
    on the next action. Corrupt a NON-actor so it voids on receipt when its
    digest disagrees with the acting peer's attached snapshot."""
    bus, sessions, order = make_table(3)
    actor = sessions[order[0]]._replica.actor
    victim_idx = next(i for i in range(3) if i != actor)
    honest_idx = next(i for i in range(3) if i != actor and i != victim_idx)
    victim = sessions[order[victim_idx]]
    victim._replica.engine.players[honest_idx].stack += 1     # silent corruption
    sessions[order[actor]].send_bet_action("call")
    bus.drain()
    assert victim.hand_voided
    assert "desync" in victim.void_reason
    # A single detector fails the whole n-of-n hand closed; nobody may keep
    # betting on a state one participant has rejected.
    assert all(sessions[cid].hand_voided for cid in order)


def test_bet_action_seat_spoof_dropped():
    bus, sessions, order = make_table(3)
    actor = sessions[order[0]]._replica.actor
    spoofer = order[(actor + 1) % 3]                    # not the actor
    forged = {"type": "bet_action", "seq": 0, "seat": actor,
              "action": "fold", "amount": 0}
    sessions[order[0]].handle_message(spoofer, forged)  # claims actor's seat
    assert sessions[order[0]]._replica.next_seq == 0    # dropped, not applied
    assert not sessions[order[0]].hand_voided


def test_deal_cheat_voids_hand_with_attribution():
    """A lying decryptor in the deal aborts the coordinator; the session
    pump converts that into a voided hand naming the seat."""
    bus, sessions, order = make_table(3)
    victim = sessions[order[0]]
    # forge a bad share at seat 0's coordinator: a deal_share with a
    # garbage proof, attributed to seat 2
    bad = {"type": "deal_share", "position": 0, "seat_from": 2,
           "D_hex": "00" * 32, "dleq_hex": "11" * 64}
    victim.handle_message("peer2", bad)
    assert victim.hand_voided
    assert "seat 2" in victim.void_reason


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
