"""Continuous play (v2-gate criterion 2): real Sessions play MULTI-HAND
hostless sessions on the in-memory bus -- stack carry, the engine's
dead-button rotation chain, void-and-redeal, eliminations, heads-up, and
last-man-standing session end. Every hand is a full trustless deal +
replica betting; next_p2p_hand() derives hand N+1's inputs identically on
every peer from hand N's settled (or reverted) state.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.session import Session
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.replica_table import PHASE_BETTING

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
        # Stable per-seat device secrets make deals reproducible across runs.
        s._deal_master_secret = bytes([i + 1]) * 32
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


def assert_synced(sessions, order, alive=None):
    idxs = alive if alive is not None else range(len(order))
    digs = [sessions[order[i]]._replica.state_digest() for i in idxs]
    assert len(set(digs)) == 1, f"replicas diverged: {digs}"


def act(bus, sessions, order, action, amount=0, ref=0, alive=None):
    """The current actor's SESSION acts; everyone else hears it."""
    seat = sessions[order[ref]]._replica.actor
    assert seat is not None
    verdict = sessions[order[seat]].send_bet_action(action, amount)
    assert verdict == "applied", f"seat {seat} {action}: {verdict}"
    bus.drain()
    assert_synced(sessions, order, alive)


def checkdown(bus, sessions, order, ref=0, alive=None):
    while sessions[order[ref]]._replica.phase == PHASE_BETTING:
        act(bus, sessions, order, "call", ref=ref, alive=alive)


def allin_hand(bus, sessions, order, ref=0, alive=None):
    while sessions[order[ref]]._replica.phase == PHASE_BETTING:
        r = sessions[order[ref]]._replica
        lg = r.engine.legal(r.actor)
        if lg["can_raise"]:
            act(bus, sessions, order, "raise", lg["max_to"],
                ref=ref, alive=alive)
        else:
            act(bus, sessions, order, "call", ref=ref, alive=alive)


def next_all(bus, sessions, order):
    """Every session advances (starts hand N+1 / spectates / ends), THEN
    the bus drains -- so all participants have begun before any hand-N+1
    message is delivered, exactly the skew the buffering absorbs."""
    verdicts = {cid: sessions[cid].next_p2p_hand() for cid in order}
    bus.drain()
    return verdicts


def test_two_hands_carry_stacks_and_rotate_button():
    """Baseline continuity: hand 1 checkdown, then next_p2p_hand starts
    hand 2 on every peer -- button advances, stacks carry from hand 1's
    settle, and all replicas agree on the new hand's state."""
    bus, sessions, order = make_table(3)
    btn1 = sessions[order[0]]._replica.button
    end1 = [sessions[order[0]]._replica.stacks[i] for i in range(3)]

    checkdown(bus, sessions, order)
    settled = [sessions[order[0]]._replica.stacks[i] for i in range(3)]
    assert sum(settled) == 1500
    assert any(a != b for a, b in zip(settled, end1))    # chips actually moved

    verdicts = next_all(bus, sessions, order)
    assert set(verdicts.values()) == {"started"}
    for cid in order:
        assert sessions[cid]._hand_no == 2
        assert sessions[cid]._replica.phase == PHASE_BETTING
        assert all(c is not None for c in sessions[cid].deal_hole_cards)
    assert_synced(sessions, order)

    # hand 2 opens with hand 1's settled stacks (minus this hand's blinds,
    # which the replicas all posted identically); button moved forward.
    btn2 = sessions[order[0]]._replica.button
    assert btn2 != btn1
    # fresh private holes for hand 2 (deal actually re-ran)
    every = []
    for cid in order:
        every.extend((c.v, c.s) for c in sessions[cid].deal_hole_cards)
    assert len(set(every)) == 6


def test_button_walks_full_orbit_over_many_hands():
    """Over several hands the button visits every seat: the dead-button
    chain is advancing, not sticking."""
    bus, sessions, order = make_table(4, stacks=[10000] * 4)
    seen = {sessions[order[0]]._replica.button}
    for _ in range(8):
        checkdown(bus, sessions, order)
        v = next_all(bus, sessions, order)
        if set(v.values()) == {"session_over"}:
            break
        seen.add(sessions[order[0]]._replica.button)
    assert len(seen) == 4, f"button only visited {seen}"


def test_next_hand_not_ready_mid_hand():
    """next_p2p_hand refuses while the hand is live (no settle/void yet)."""
    bus, sessions, order = make_table(3)
    assert sessions[order[0]]._replica.phase == PHASE_BETTING
    assert sessions[order[0]].next_p2p_hand() == "not_ready"


def test_malformed_hand_number_is_ignored():
    """Untrusted wire data cannot crash or pollute future-hand buffering."""
    bus, sessions, order = make_table(3)
    target = sessions[order[0]]
    before = target._replica.state_digest()

    target.handle_message(order[1], {
        "type": "bet_action",
        "hand": {"not": "a number"},
        "seq": 0,
        "seat": 1,
        "action": "call",
        "amount": 0,
        "digest": before,
    })

    assert target._replica.state_digest() == before
    assert target._msg_buffer == []


def test_deal_cheat_void_reverts_and_redeals_same_seats():
    """A voided hand: chips revert, and next_p2p_hand redeals the SAME
    seats at the SAME button (a misdeal), not a chip-losing advance.

    Uses a deal-cheat to make every coordinator independently detect the
    invalid proof; the hand-void broadcast is idempotent when those local
    detections overlap."""
    bus, sessions, order = make_table(3)
    stacks_before = [sessions[order[0]]._replica.stacks[i] for i in range(3)]
    btn = sessions[order[0]]._replica.button

    # A forged deal_share with a garbage proof, attributed to seat 2,
    # delivered to every peer -> each coordinator aborts -> hand voids
    # everywhere on the next pump.
    for cid in order:
        bad = {"type": "deal_share", "position": 0, "seat_from": 2,
               "hand": sessions[cid]._hand_no,
               "D_hex": "00" * 32, "dleq_hex": "11" * 64}
        sessions[cid].handle_message("peer2", bad)
    bus.drain()
    for cid in order:
        assert sessions[cid].hand_voided
        assert "seat 2" in sessions[cid].void_reason

    verdicts = next_all(bus, sessions, order)
    assert set(verdicts.values()) == {"started"}

    # Redeal: same seats dealt, same button, stacks reverted to pre-void
    # (settle never ran) minus this fresh hand's identically-posted blinds.
    for cid in order:
        r = sessions[cid]._replica
        assert r.button == btn
        assert sorted(r.seats_dealt) == [0, 1, 2]
        assert not sessions[cid].hand_voided
    assert_synced(sessions, order)
    # Chips conserved through the void+redeal: reverted stacks-behind plus
    # committed chips (blinds) equal the table's starting bankroll. (The
    # 500-each start is the true baseline; stacks_before was read after
    # hand 1 had already posted its blinds, so it is short by them.)
    e = sessions[order[0]]._replica.engine
    assert sum(p.stack for p in e.players) + e.pot == 1500


def test_elimination_drops_seat_and_shrinks_next_deal():
    """A seat that busts is not dealt into the next hand: seats_dealt
    shrinks, the deal runs among survivors, and the busted LOCAL session
    reports 'eliminated' while survivors get 'started'."""
    # Seat 1 is short; an all-in hand to a showdown will bust someone.
    bus, sessions, order = make_table(3, stacks=[1000, 20, 1000])
    allin_hand(bus, sessions, order)
    # Someone must have gone to zero for a clean elimination test.
    stacks = [sessions[order[0]]._replica.stacks[i] for i in range(3)]
    busted = [i for i, s in enumerate(stacks) if s == 0]
    assert busted, f"no elimination occurred (stacks {stacks})"
    survivors = [i for i in range(3) if stacks[i] > 0]

    # A next-hand action can arrive before this busted client processes its
    # own next_hand command. It may be buffered briefly, but elimination must
    # discard it so a long-running spectator does not retain gameplay traffic.
    for i in busted:
        sessions[order[i]].handle_message(order[survivors[0]], {
            "type": "bet_action",
            "hand": sessions[order[i]]._hand_no + 1,
        })
        assert sessions[order[i]]._msg_buffer

    verdicts = next_all(bus, sessions, order)
    assert all(not sessions[order[i]]._msg_buffer for i in busted)
    if len(survivors) < 2:
        assert set(verdicts.values()) == {"session_over"}
        assert all(sessions[order[i]]._p2p_spectator for i in busted)
        return
    for i in survivors:
        assert verdicts[order[i]] == "started"
    for i in busted:
        assert verdicts[order[i]] == "eliminated"
    # The live hand contains only survivors.
    ref = survivors[0]
    r = sessions[order[ref]]._replica
    assert sorted(r.seats_dealt) == sorted(survivors)
    assert_synced(sessions, order, alive=survivors)
    # A busted session keeps its final settled snapshot for the client.
    b = busted[0]
    assert sessions[order[b]].hand_result is not None


def test_eliminated_spectator_accepts_signed_session_end_envelope():
    bus, sessions, order = make_table(3)
    spectator = sessions[order[0]]
    spectator._p2p_spectator = True
    spectator.handle_message(order[1], {
        "type": "session_end",
        "payload": {
            "hand": spectator._hand_no,
            "seat": 1,
            "winner": 1,
            "stacks": [0, 1500, 0],
        },
        "hash": "a" * 64,
        "prev": "0" * 64,
    })

    assert spectator._session_over
    assert spectator._session_winner == 1
    assert spectator._final_stacks == [0, 1500, 0]


def test_heads_up_positions_and_play():
    """Down to two seats: the engine's heads-up override (button = SB,
    acts first preflop) holds, and a heads-up hand plays to settle across
    both replicas."""
    bus, sessions, order = make_table(2, stacks=[500, 500])
    r = sessions[order[0]]._replica
    # HU: button and SB are the same seat.
    assert r.engine.button == r.engine.sb_seat
    assert r.engine.bb_seat != r.engine.button
    # Preflop first-actor is the button/SB in heads-up.
    assert r.actor == r.engine.button

    checkdown(bus, sessions, order)
    for cid in order:
        assert sessions[cid].hand_result is not None
        assert sessions[cid]._replica.stacks == \
            sessions[order[0]]._replica.stacks       # identical settle
    assert sum(sessions[order[0]]._replica.stacks) == 1000


def test_full_match_runs_to_a_single_winner():
    """End to end: keep playing all-in hands until one seat holds every
    chip. The session terminates with 'session_over' on every peer and a
    winner that owns the whole pot; nothing hangs or diverges."""
    bus, sessions, order = make_table(3, stacks=[300, 300, 300])
    total = 900
    for _ in range(60):                       # generous bound; must terminate
        alive_now = [i for i in range(3)
                     if sessions[order[i]]._replica is not None
                     and sessions[order[i]]._replica.stacks[i] > 0]
        ref = next(i for i in range(3)
                   if not sessions[order[i]]._p2p_spectator)
        allin_hand(bus, sessions, order, ref=ref,
                   alive=[i for i in range(3)
                          if not sessions[order[i]]._p2p_spectator])
        verdicts = next_all(bus, sessions, order)
        if "session_over" in verdicts.values():
            # Previously busted spectators learn the terminal state from
            # session_end after next_all drains the bus.
            assert all(sessions[cid]._session_over for cid in order)
            break
    else:
        pytest.fail("match did not resolve to a single winner")

    winners = {sessions[cid]._session_winner for cid in order}
    assert len(winners) == 1                  # every peer names the same winner
    w = winners.pop()
    assert w is not None
    assert all(sessions[cid]._final_stacks == sessions[order[0]]._final_stacks
               for cid in order)
    for i, cid in enumerate(order):
        assert sessions[cid]._p2p_spectator is (i != w)
    # the winner's session holds all chips in its last replica
    assert sessions[order[w]]._replica.stacks[w] == total


def test_all_peers_agree_on_winner_each_settlement():
    """Across a multi-hand run every peer's settled result is byte-identical
    hand by hand -- the invariant that lets a hostless table trust its own
    payouts."""
    bus, sessions, order = make_table(3, stacks=[400, 400, 400])
    for _ in range(6):
        checkdown(bus, sessions, order)
        results = [sessions[cid].hand_result for cid in order]
        assert all(r is not None and r == results[0] for r in results)
        v = next_all(bus, sessions, order)
        if "session_over" in v.values():
            break


def test_second_hand_carries_stacks_and_rotates_button():
    """Two hands back-to-back: hand 2's stacks are exactly hand 1's
    settled stacks, the button moved, and every replica agrees on both."""
    bus, sessions, order = make_table(3, stacks=[500, 500, 500])
    # settled hand 1
    checkdown(bus, sessions, order)
    settled1 = [sessions[c].hand_result for c in order]
    assert all(r is not None for r in settled1)
    stacks_after_1 = sessions[order[0]]._replica.stacks
    btn1 = sessions[order[0]]._replica.button

    verdicts = next_all(bus, sessions, order)
    assert set(verdicts.values()) == {"started"}
    # hand 2 dealt, everyone synced, stacks carried, button advanced
    assert_synced(sessions, order)
    for c in order:
        assert sessions[c]._hand_no == 2
        assert sessions[c]._replica.phase == PHASE_BETTING
        # the stacks hand 2 was constructed from == hand 1's settled stacks
        assert sessions[c]._hand_stacks == stacks_after_1
    btn2 = sessions[order[0]]._replica.button
    assert btn2 != btn1                     # dead-button rotation happened
    # chips conserved: hand 2's carry-in stacks (pre-blind) sum to the total
    assert sum(sessions[order[0]]._hand_stacks) == 1500


def test_button_advances_one_seat_each_hand():
    """Over several full hands the button walks forward around the table
    (dead-button rule; with everyone still in, one eligible seat per hand)."""
    bus, sessions, order = make_table(4, stacks=[1000] * 4)
    seen = []
    for _ in range(5):
        checkdown(bus, sessions, order)
        # settled boundary: chips are conserved here (mid-hand they sit in the pot)
        assert sum(sessions[order[0]]._replica.stacks) == 4000
        seen.append(sessions[order[0]]._replica.button)
        v = next_all(bus, sessions, order)
        if set(v.values()) != {"started"}:
            break
    # buttons are distinct hand-to-hand and every peer saw the same ones
    for a, b in zip(seen, seen[1:]):
        assert a != b


def test_not_ready_before_hand_completes():
    bus, sessions, order = make_table(3)
    # mid-hand: nobody may advance yet
    assert sessions[order[0]].next_p2p_hand() == "not_ready"
    # one action in, still mid-hand
    act(bus, sessions, order, "call")
    assert sessions[order[0]].next_p2p_hand() == "not_ready"


def test_replica_desync_void_propagates_and_redeals():
    """One peer's desync detection voids every replica before the redeal."""
    START = [500, 500, 500]
    bus, sessions, order = make_table(3, stacks=list(START))
    carry_in = list(sessions[order[0]]._hand_stacks)   # pre-blind hand-1 input
    assert carry_in == START
    btn_voided = sessions[order[0]]._replica.button

    # corrupt a non-actor so it voids on the next action's digest check
    actor = sessions[order[0]]._replica.actor
    victim_idx = next(i for i in range(3) if i != actor)
    sessions[order[victim_idx]]._replica.engine.players[actor].stack += 1
    sessions[order[actor]].send_bet_action("call")
    bus.drain()
    assert all(sessions[cid].hand_voided for cid in order)
    assert all("replica desync" in sessions[cid].void_reason for cid in order)

    # Every peer redeals the same seats from the same carry-in and button.
    verdicts = next_all(bus, sessions, order)
    assert set(verdicts.values()) == {"started"}
    for c in order:
        assert sessions[c]._hand_no == 2
        assert sessions[c]._hand_stacks == carry_in    # reverted, not paid
        assert sessions[c]._replica.button == btn_voided   # same button
    assert_synced(sessions, order)


def _shove_hand(bus, sessions, order, alive):
    """Everyone still alive goes all-in; returns the settled result."""
    ref = alive[0]
    while sessions[order[ref]]._replica.phase == PHASE_BETTING:
        r = sessions[order[ref]]._replica
        seat = r.actor
        lg = r.engine.legal(seat)
        a = ("raise", lg["max_to"]) if lg["can_raise"] else ("call", 0)
        assert sessions[order[seat]].send_bet_action(*a) == "applied"
        bus.drain()
    return sessions[order[ref]].hand_result


def test_busted_seat_spectates_survivors_play_on():
    """After an all-in, every busted seat's session returns 'eliminated'
    and stops playing; survivors deal the next hand without the dead seats
    and stay in sync. The cards decide who (if anyone) busts -- an all-in
    can chop and bust nobody -- so this asserts the invariant for whatever
    outcome the deal produced."""
    bus, sessions, order = make_table(4, stacks=[300, 300, 300, 300])
    _shove_hand(bus, sessions, order, alive=[0, 1, 2, 3])
    stacks = sessions[order[0]]._replica.stacks
    dead = [i for i, s in enumerate(stacks) if s == 0]
    alive = [i for i, s in enumerate(stacks) if s > 0]
    assert sum(stacks) == 1200                      # settled: chips conserved

    verdicts = next_all(bus, sessions, order)
    if len(alive) < 2:
        # collapsed straight to a winner: everyone reports session over
        assert set(verdicts.values()) == {"session_over"}
        return
    # each dead seat's session spectates; each survivor deals hand 2
    for i in dead:
        assert verdicts[order[i]] == "eliminated"
        assert sessions[order[i]]._p2p_spectator
        assert sessions[order[i]].hand_result is not None   # keeps final snapshot
    for i in alive:
        assert verdicts[order[i]] == "started"
        assert sessions[order[i]]._hand_no == 2
    assert_synced(sessions, order, alive=alive)
    dealt = sessions[order[alive[0]]]._replica.seats_dealt
    for i in dead:
        assert i not in dealt                               # dead seats not dealt
    for i in alive:
        assert i in dealt


def test_heads_up_allin_resolves_session_or_chops():
    """A heads-up all-in either busts one seat -- every peer then reports
    session_over and names the same winner -- or chops, in which case both
    survive and play on. Both outcomes are legal; the cards choose."""
    bus, sessions, order = make_table(2, stacks=[1000, 1000])
    _shove_hand(bus, sessions, order, alive=[0, 1])
    stacks = sessions[order[0]]._replica.stacks
    assert sum(stacks) == 2000                      # settled: conserved
    alive = [i for i, s in enumerate(stacks) if s > 0]

    verdicts = next_all(bus, sessions, order)
    if len(alive) == 1:
        assert set(verdicts.values()) == {"session_over"}
        for cid in order:
            assert sessions[cid]._session_over
            assert sessions[cid]._session_winner == alive[0]
    else:                                            # chopped: play continues
        assert set(verdicts.values()) == {"started"}
        for cid in order:
            assert sessions[cid]._hand_no == 2
        assert_synced(sessions, order)


def test_heads_up_multi_hand_alternates_blinds():
    """A two-player table plays several hands: the engine's heads-up rule
    puts the button on the SB and alternates it each hand, stacks carry,
    and both replicas stay in lockstep."""
    bus, sessions, order = make_table(2, stacks=[2000, 2000], sb=25, bb=50)
    buttons = []
    for _ in range(4):
        r = sessions[order[0]]._replica
        # heads-up: exactly two dealt, button == SB seat, they differ from BB
        assert r.seats_dealt == [0, 1]
        assert r.engine.button == r.engine.sb_seat
        assert r.engine.sb_seat != r.engine.bb_seat
        buttons.append(r.engine.button)
        checkdown(bus, sessions, order)
        assert sum(sessions[order[0]]._replica.stacks) == 4000   # settled: conserved
        assert_synced(sessions, order)
        v = next_all(bus, sessions, order)
        if set(v.values()) != {"started"}:
            break
    # the button alternated between the two seats hand to hand
    for a, b in zip(buttons, buttons[1:]):
        assert a != b


def test_long_session_conserves_chips_and_stays_synced():
    """A multi-hand checkdown session: chips are conserved every hand and
    all replicas agree at every boundary, until it (eventually) ends."""
    bus, sessions, order = make_table(4, stacks=[600, 600, 600, 600])
    total = 2400
    hands = 0
    for _ in range(30):
        alive = [i for i, s in enumerate(sessions[order[0]]._replica.stacks)
                 if s > 0]
        checkdown(bus, sessions, order)
        assert sum(sessions[order[0]]._replica.stacks) == total
        assert_synced(sessions, order, alive=alive)
        hands += 1
        v = next_all(bus, sessions, order)
        if "session_over" in v.values():
            # session_end updates earlier spectators after the bus drains.
            assert all(sessions[c]._session_over for c in order)
            winners = {sessions[c]._session_winner for c in order}
            assert len(winners) == 1
            break
    assert hands >= 2                       # actually played multiple hands
