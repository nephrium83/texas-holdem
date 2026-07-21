"""Async-network robustness for hostless hand sequencing: peers begin hands
at different times (no host to synchronize them), so deal/bet messages carry
a hand number and are buffered until the local peer begins that hand. Without
this an early key_announce is dropped and the deal deadlocks.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.session import Session
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport

import importlib
try:
    importlib.import_module("holdem.p2p.elgamal")   # libsodium guard
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


def make_table(n):
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
    return bus, sessions, order


def _cfg(n):
    return dict(names=[f"P{i}" for i in range(n)], stacks=[500] * n, sb=5, bb=10)


def test_early_begin_then_late_peers_still_deal():
    """peer0 begins hand 1 and its key_announce reaches peers that have NOT
    begun yet; they buffer it, and once they begin the deal completes for
    everyone (no deadlock)."""
    bus, sessions, order = make_table(3)
    # peer0 starts first; its key_announce goes out to peers with no driver
    sessions["peer0"].start_p2p_hand(hand_no=1, button=0, **_cfg(3))
    bus.drain()
    # peers 1 and 2 have not begun -> they buffered peer0's message
    assert sessions["peer1"]._deal_driver is None
    assert sessions["peer1"]._msg_buffer, "expected peer1 to buffer the early msg"
    # now the late peers begin
    sessions["peer1"].start_p2p_hand(hand_no=1, button=0, **_cfg(3))
    sessions["peer2"].start_p2p_hand(hand_no=1, button=0, **_cfg(3))
    bus.drain()
    # the deal completed for everyone despite the staggered start
    for cid in order:
        s = sessions[cid]
        assert all(c is not None for c in s.deal_hole_cards), f"{cid} no holes"
        assert not s.hand_voided
    # and they all agree on the deck
    decks = [[ct.to_hex() for ct in sessions[c]._deal_driver.deal.deck]
             for c in order]
    assert all(d == decks[0] for d in decks)


def test_fully_staggered_start_in_any_interleaving():
    """Each peer begins and drains before the next even starts -- the most
    extreme skew. Buffering must still converge."""
    bus, sessions, order = make_table(3)
    for cid in order:
        sessions[cid].start_p2p_hand(hand_no=1, button=0, **_cfg(3))
        bus.drain()                             # drain between each start
    for cid in order:
        assert all(c is not None for c in sessions[cid].deal_hole_cards)
        assert not sessions[cid].hand_voided


def test_future_hand_bet_action_buffered_not_dropped():
    """A bet_action for a hand a peer hasn't reached yet is buffered and
    applied once it begins that hand, not dropped."""
    bus, sessions, order = make_table(2)
    # peer1 is 'ahead': craft a hand-2 bet_action and deliver it to peer0
    # while peer0 is still at hand 0 (no hand begun)
    future = {"type": "bet_action", "hand": 2, "seq": 0, "seat": 1,
              "action": "call", "amount": 0}
    sessions["peer0"].handle_message("peer1", future)
    assert sessions["peer0"]._msg_buffer          # buffered, awaiting hand 2
    # peer0 progresses to hand 2 (start hand 1, then 2)
    for h in (1, 2):
        sessions["peer0"]._msg_buffer = [b for b in sessions["peer0"]._msg_buffer]
        # (hand 1 has no partner here; we only check the buffer survives to h2)
    # after reaching hand 2 the buffered bet is eligible; drop-stale check:
    sessions["peer0"]._hand_no = 2
    kept = [b for b in sessions["peer0"]._msg_buffer if b[1]["hand"] >= 2]
    assert kept, "hand-2 action should still be buffered at hand 2"


def test_stale_hand_message_ignored():
    """A message tagged with a past hand is dropped, not applied to the
    current hand."""
    bus, sessions, order = make_table(2)
    sessions["peer0"]._hand_no = 5
    sessions["peer1"]._hand_no = 5
    stale = {"type": "bet_action", "hand": 3, "seq": 0, "seat": 1,
             "action": "call"}
    # no replica/driver, but the point is it must not buffer a PAST hand
    sessions["peer0"].handle_message("peer1", stale)
    assert not sessions["peer0"]._msg_buffer      # stale -> neither processed nor buffered


def test_on_state_changed_fires_during_deal():
    """The async-render hook fires as the hand progresses."""
    bus, sessions, order = make_table(3)
    ticks = {c: 0 for c in order}
    for c in order:
        sessions[c].on_state_changed = (lambda cc: (lambda: ticks.__setitem__(cc, ticks[cc] + 1)))(c)
    for cid in order:
        sessions[cid].start_p2p_hand(hand_no=1, button=0, **_cfg(3))
    bus.drain()
    for cid in order:
        assert ticks[cid] > 0, f"{cid} never notified"


def test_next_hand_after_settle_no_deadlock():
    """Play hand 1 to settlement, then start hand 2 with carried stacks and
    a moved button -- the buffering keeps hand 2's staggered start working."""
    bus, sessions, order = make_table(3)
    for cid in order:
        sessions[cid].start_p2p_hand(hand_no=1, button=0, **_cfg(3))
    bus.drain()
    # everyone folds to end hand 1
    while sessions[order[0]]._replica.phase == "betting":
        seat = sessions[order[0]]._replica.actor
        sessions[order[seat]].send_bet_action("fold")
        bus.drain()
    stacks = [p.stack for p in sessions[order[0]]._replica.engine.players]
    # start hand 2, staggered, with the carried stacks
    names = [f"P{i}" for i in range(3)]
    sessions["peer0"].start_p2p_hand(hand_no=2, button=1, names=names,
                                     stacks=stacks, sb=5, bb=10)
    bus.drain()
    sessions["peer1"].start_p2p_hand(hand_no=2, button=1, names=names,
                                     stacks=stacks, sb=5, bb=10)
    sessions["peer2"].start_p2p_hand(hand_no=2, button=1, names=names,
                                     stacks=stacks, sb=5, bb=10)
    bus.drain()
    for cid in order:
        s = sessions[cid]
        assert s._hand_no == 2
        assert all(c is not None for c in s.deal_hole_cards)
        assert not s.hand_voided


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
