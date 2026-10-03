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

from holdem import client_view
from holdem.p2p.session import Session
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.replica_table import PHASE_BETTING
from tests.showdown_rig import rig_showdowns

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
        # Stable per-seat device secrets keep the KEY ceremony reproducible.
        # The deal itself is not: shuffle_mp draws a fresh CSPRNG
        # permutation and fresh re-encryption scalars every run, as it
        # must. Tests here may not assume a particular hand outcome.
        s._deal_master_secret = bytes([i + 1]) * 32
        s.local_conn_id = cid
        s.configure_seats(list(order))
        s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
        bus.register(cid, s)
        sessions[cid] = s
    names = [f"P{i}" for i in range(n)]
    stacks = list(stacks) if stacks else [500] * n
    for i, cid in enumerate(order):
        if stacks[i] == 0:
            continue                  # never dealt: it has no hand to start
        sessions[cid].start_p2p_hand(hand_no=hand, names=names,
                                     stacks=stacks, sb=sb, bb=bb,
                                     button=button)
    bus.drain()
    return bus, sessions, order


def assert_synced(sessions, order, alive=None):
    idxs = alive if alive is not None else range(len(order))
    digs = [sessions[order[i]].replica.state_digest() for i in idxs]
    assert len(set(digs)) == 1, f"replicas diverged: {digs}"


def act(bus, sessions, order, action, amount=0, ref=0, alive=None):
    """The current actor's SESSION acts; everyone else hears it."""
    seat = sessions[order[ref]].replica.actor
    assert seat is not None
    verdict = sessions[order[seat]].send_bet_action(action, amount)
    assert verdict == "applied", f"seat {seat} {action}: {verdict}"
    bus.drain()
    assert_synced(sessions, order, alive)


def checkdown(bus, sessions, order, ref=0, alive=None):
    while sessions[order[ref]].replica.phase == PHASE_BETTING:
        act(bus, sessions, order, "call", ref=ref, alive=alive)


def allin_hand(bus, sessions, order, ref=0, alive=None):
    while sessions[order[ref]].replica.phase == PHASE_BETTING:
        r = sessions[order[ref]].replica
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
    btn1 = sessions[order[0]].replica.button
    end1 = [sessions[order[0]].replica.stacks[i] for i in range(3)]

    checkdown(bus, sessions, order)
    settled = [sessions[order[0]].replica.stacks[i] for i in range(3)]
    assert sum(settled) == 1500
    assert any(a != b for a, b in zip(settled, end1))    # chips actually moved

    verdicts = next_all(bus, sessions, order)
    assert set(verdicts.values()) == {"started"}
    for cid in order:
        assert sessions[cid]._hand_no == 2
        assert sessions[cid].replica.phase == PHASE_BETTING
        assert all(c is not None for c in sessions[cid].deal_hole_cards)
    assert_synced(sessions, order)

    # hand 2 opens with hand 1's settled stacks (minus this hand's blinds,
    # which the replicas all posted identically); button moved forward.
    btn2 = sessions[order[0]].replica.button
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
    seen = {sessions[order[0]].replica.button}
    for _ in range(8):
        checkdown(bus, sessions, order)
        v = next_all(bus, sessions, order)
        if set(v.values()) == {"session_over"}:
            break
        seen.add(sessions[order[0]].replica.button)
    assert len(seen) == 4, f"button only visited {seen}"


def test_next_hand_not_ready_mid_hand():
    """next_p2p_hand refuses while the hand is live (no settle/void yet)."""
    bus, sessions, order = make_table(3)
    assert sessions[order[0]].replica.phase == PHASE_BETTING
    assert sessions[order[0]].next_p2p_hand() == "not_ready"


def test_malformed_hand_number_is_ignored():
    """Untrusted wire data cannot crash or pollute future-hand buffering."""
    bus, sessions, order = make_table(3)
    target = sessions[order[0]]
    before = target.replica.state_digest()

    target.handle_message(order[1], {
        "type": "bet_action",
        "hand": {"not": "a number"},
        "seq": 0,
        "seat": 1,
        "action": "call",
        "amount": 0,
        "digest": before,
    })

    assert target.replica.state_digest() == before
    assert target._msg_buffer == []


def test_deal_cheat_void_reverts_and_redeals_same_seats():
    """A voided hand: chips revert, and next_p2p_hand redeals the SAME
    seats at the SAME button (a misdeal), not a chip-losing advance.

    Uses a deal-cheat to make every coordinator independently detect the
    invalid proof; the hand-void broadcast is idempotent when those local
    detections overlap."""
    bus, sessions, order = make_table(3)
    btn = sessions[order[0]].replica.button

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
        r = sessions[cid].replica
        assert r.button == btn
        assert sorted(r.seats_dealt) == [0, 1, 2]
        assert not sessions[cid].hand_voided
    assert_synced(sessions, order)
    # Chips conserved through the void+redeal: reverted stacks-behind plus
    # committed chips (blinds) equal the table's starting bankroll. (The
    # 500-each start is the true baseline; stacks_before was read after
    # hand 1 had already posted its blinds, so it is short by them.)
    e = sessions[order[0]].replica.engine
    assert sum(p.stack for p in e.players) + e.pot == 1500


def test_elimination_drops_seat_and_shrinks_next_deal():
    """A seat that busts is not dealt into the next hand: seats_dealt
    shrinks, the deal runs among survivors, and the busted LOCAL session
    reports 'eliminated' while survivors get 'started'."""
    # Seat 1 is short and everyone goes all in, so a showdown usually busts
    # someone -- but not always: if the pot chops every way the table
    # survives intact. The shuffle is genuinely random (see make_table), so
    # that is a real outcome, and asserting it cannot happen made this test
    # fail intermittently. Retry until the PRECONDITION holds; the
    # behaviour under test below is still asserted at full strength.
    for attempt in range(20):
        bus, sessions, order = make_table(3, stacks=[1000, 20, 1000])
        allin_hand(bus, sessions, order)
        stacks = [sessions[order[0]].replica.stacks[i] for i in range(3)]
        busted = [i for i, s in enumerate(stacks) if s == 0]
        if busted:
            break
    else:
        pytest.fail(f"no elimination in {attempt + 1} all-in hands "
                    f"(last stacks {stacks})")
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
    r = sessions[order[ref]].replica
    assert sorted(r.seats_dealt) == sorted(survivors)
    assert_synced(sessions, order, alive=survivors)
    # A busted session keeps its final settled snapshot for the client.
    b = busted[0]
    assert sessions[order[b]].hand_result is not None


def test_eliminated_spectator_accepts_signed_session_end_envelope():
    """The match ends in a hand after the spectator's last, the one hand
    a session_end can name for it (see the test below)."""
    bus, sessions, order = make_table(3)
    spectator = sessions[order[0]]
    spectator._p2p_spectator = True
    spectator.handle_message(order[1], {
        "type": "session_end",
        "payload": {
            "hand": spectator._hand_no + 1,
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


# A seated peer that is still playing checks a session_end against its own
# settlement: same hand, already settled here, identical stacks. Each case
# below would have passed the old shape-and-total checks.

def test_a_seated_peer_ignores_a_session_end_sent_mid_hand():
    """The old hole: any seat could end the match mid-hand and name itself
    the winner, with stacks that merely summed to the table total."""
    bus, sessions, order = make_table(3)
    target = sessions[order[0]]
    before = target.replica.state_digest()
    target.handle_message(order[1], {"type": "session_end", "hand": 1,
                                     "seat": 1, "winner": 1,
                                     "stacks": [0, 1500, 0]})
    assert target.terminal_state is None
    assert not target._session_over
    assert target.replica.state_digest() == before


def _heads_up_bust():
    """A heads-up all-in that busts a seat. The shuffle is random and an
    all-in can chop, so retry until the precondition holds."""
    for _ in range(20):
        bus, sessions, order = make_table(2, stacks=[500, 500])
        _shove_hand(bus, sessions, order, alive=[0, 1])
        stacks = sessions[order[0]].replica.stacks
        if 0 in stacks:
            return bus, sessions, order, stacks
    pytest.fail("no heads-up all-in busted a seat in 20 tries")


def test_a_seated_peer_accepts_a_session_end_matching_its_settlement():
    """The honest path: the winner ends the match before the loser has
    called next_p2p_hand. The loser has settled the same hand with the
    same stacks, so it accepts."""
    bus, sessions, order, stacks = _heads_up_bust()
    winner, loser = (0, 1) if stacks[0] else (1, 0)
    assert sessions[order[winner]].next_p2p_hand() == "session_over"
    bus.drain()
    s = sessions[order[loser]]
    assert s.terminal_state == Session.ENDED_NORMAL
    assert s._final_stacks == stacks
    assert s._session_winner == winner


@pytest.mark.parametrize("change", ["later-hand", "other-stacks"])
def test_a_seated_peer_ignores_a_session_end_unlike_its_settlement(change):
    bus, sessions, order, stacks = _heads_up_bust()
    winner, loser = (0, 1) if stacks[0] else (1, 0)
    claim = {"type": "session_end", "hand": 1, "seat": winner,
             "winner": winner, "stacks": list(stacks)}
    if change == "later-hand":
        claim["hand"] = 2
    else:                                         # the loser named winner
        claim["stacks"] = list(reversed(stacks))
        claim["winner"] = loser
    s = sessions[order[loser]]
    s.handle_message(order[winner], claim)
    assert s.terminal_state is None
    assert not s._session_over


def test_a_seat_busted_before_pressing_next_accepts_the_later_end(
        monkeypatch):
    """Seat 2 busts in hand 1 and never presses Next. Seats 0 and 1 play
    hand 2 to a winner, whose session_end names hand 2 -- a hand seat 2
    was never dealt. It stopped following hands at its bust, as a
    spectator does, so it checks the notice as one does. Ignoring it, as a
    seat still playing would, left it waiting for good: the notice is
    sent once, and Next would only report it eliminated."""
    rank = rig_showdowns(monkeypatch)
    rank(1, 2, 0)
    bus, sessions, order = make_table(3, stacks=[500, 500, 20])
    _shove_hand(bus, sessions, order, alive=[0, 1, 2], folding=[0])
    # Seat 1 opens all in, seat 2 calls all in for 20 and seat 0 folds its
    # big blind: seat 1's aces take 20 + 20 + 10.
    assert sessions[order[0]].replica.stacks == [490, 530, 0]
    busted = sessions[order[2]]
    assert {sessions[c].next_p2p_hand() for c in order[:2]} == {"started"}
    bus.drain()
    rank(1, 0, 2)
    _shove_hand(bus, sessions, order, alive=[0, 1])
    assert sessions[order[0]].replica.stacks == [0, 1020, 0]
    assert busted.terminal_state is None

    assert sessions[order[1]].next_p2p_hand() == "session_over"
    bus.drain()

    for s in (busted, sessions[order[0]]):
        assert s.terminal_state == Session.ENDED_NORMAL
        assert s._final_stacks == [0, 1020, 0]
        assert s._session_winner == 1
    assert client_view.snapshot(busted)["turn"]["state"] == "match_complete"
    assert busted.next_p2p_hand() == "session_over"


@pytest.mark.parametrize("pressed_next", [False, True])
def test_a_busted_seat_ignores_a_session_end_for_the_hand_it_busted_in(
        monkeypatch, pressed_next):
    """Seat 2 busts in hand 1 while seats 0 and 1 keep chips, so hand 1
    ended no match. Seat 0 nonetheless claims it won it all in hand 1.
    Seat 2 settled hand 1 itself and can see the claim is false, so its
    relaxed check for later hands does not apply."""
    rig_showdowns(monkeypatch)(1, 2, 0)
    bus, sessions, order = make_table(3, stacks=[500, 500, 20])
    _shove_hand(bus, sessions, order, alive=[0, 1, 2], folding=[0])
    busted = sessions[order[2]]
    assert busted.replica.stacks == [490, 530, 0]
    if pressed_next:
        assert busted.next_p2p_hand() == "eliminated"
    claim = {"type": "session_end", "hand": 1, "seat": 0, "winner": 0,
             "stacks": [1020, 0, 0]}
    for cid in order[1:]:
        sessions[cid].handle_message(order[0], dict(claim))
        assert sessions[cid].terminal_state is None, cid
        assert not sessions[cid]._session_over


def test_heads_up_positions_and_play():
    """Down to two seats: the engine's heads-up override (button = SB,
    acts first preflop) holds, and a heads-up hand plays to settle across
    both replicas."""
    bus, sessions, order = make_table(2, stacks=[500, 500])
    r = sessions[order[0]].replica
    # HU: button and SB are the same seat.
    assert r.engine.button == r.engine.sb_seat
    assert r.engine.bb_seat != r.engine.button
    # Preflop first-actor is the button/SB in heads-up.
    assert r.actor == r.engine.button

    checkdown(bus, sessions, order)
    for cid in order:
        assert sessions[cid].hand_result is not None
        assert sessions[cid].replica.stacks == \
            sessions[order[0]].replica.stacks       # identical settle
    assert sum(sessions[order[0]].replica.stacks) == 1000


def test_full_match_runs_to_a_single_winner():
    """End to end: keep playing all-in hands until one seat holds every
    chip. The session terminates with 'session_over' on every peer and a
    winner that owns the whole pot; nothing hangs or diverges."""
    bus, sessions, order = make_table(3, stacks=[300, 300, 300])
    total = 900
    for _ in range(60):                       # generous bound; must terminate
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
    assert sessions[order[w]].replica.stacks[w] == total


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
    stacks_after_1 = sessions[order[0]].replica.stacks
    btn1 = sessions[order[0]].replica.button

    verdicts = next_all(bus, sessions, order)
    assert set(verdicts.values()) == {"started"}
    # hand 2 dealt, everyone synced, stacks carried, button advanced
    assert_synced(sessions, order)
    for c in order:
        assert sessions[c]._hand_no == 2
        assert sessions[c].replica.phase == PHASE_BETTING
        # the stacks hand 2 was constructed from == hand 1's settled stacks
        assert sessions[c]._hand_stacks == stacks_after_1
    btn2 = sessions[order[0]].replica.button
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
        assert sum(sessions[order[0]].replica.stacks) == 4000
        seen.append(sessions[order[0]].replica.button)
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
    btn_voided = sessions[order[0]].replica.button

    # corrupt a non-actor so it voids on the next action's digest check
    actor = sessions[order[0]].replica.actor
    victim_idx = next(i for i in range(3) if i != actor)
    sessions[order[victim_idx]].replica.engine.players[actor].stack += 1
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
        assert sessions[c].replica.button == btn_voided   # same button
    assert_synced(sessions, order)


def _shove_hand(bus, sessions, order, alive, folding=()):
    """Everyone still alive goes all-in, except that the seats folding
    fold; returns the settled result."""
    ref = alive[0]
    while sessions[order[ref]].replica.phase == PHASE_BETTING:
        r = sessions[order[ref]].replica
        seat = r.actor
        lg = r.engine.legal(seat)
        a = ("raise", lg["max_to"]) if lg["can_raise"] else ("call", 0)
        if seat in folding:
            a = ("fold", 0)
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
    stacks = sessions[order[0]].replica.stacks
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
    dealt = sessions[order[alive[0]]].replica.seats_dealt
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
    stacks = sessions[order[0]].replica.stacks
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
        r = sessions[order[0]].replica
        # heads-up: exactly two dealt, button == SB seat, they differ from BB
        assert r.seats_dealt == [0, 1]
        assert r.engine.button == r.engine.sb_seat
        assert r.engine.sb_seat != r.engine.bb_seat
        buttons.append(r.engine.button)
        checkdown(bus, sessions, order)
        assert sum(sessions[order[0]].replica.stacks) == 4000   # settled: conserved
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
        alive = [i for i, s in enumerate(sessions[order[0]].replica.stacks)
                 if s > 0]
        checkdown(bus, sessions, order)
        assert sum(sessions[order[0]].replica.stacks) == total
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


# ------------------------------------------------- settlement agreement
#
# Every seat broadcasts hand_settled {hand, digest} when it settles. Equal
# digests mean equal stacks and positions -- the next hand's inputs -- so a
# mismatch, or a void of a hand another seat settled, ends the table with
# ABORTED_PROTOCOL instead of looping void-and-redeal from carry-ins that
# can never agree.

def test_every_seat_reports_the_same_settlement():
    """Every seat holds a matching report from each of the others: the
    broadcast reached it and was compared, not merely sent."""
    bus, sessions, order = make_table(3)
    checkdown(bus, sessions, order)
    digests = {sessions[c]._ended_digest for c in order}
    assert len(digests) == 1 and None not in digests
    assert digests == {sessions[order[0]].replica.state_digest()}
    assert all(sessions[c].terminal_state is None for c in order)
    assert all(sessions[c]._ended_agreed == {0, 1, 2} for c in order)


def _hold_settlement_reports(session):
    """Keep session's hand_settled broadcasts back; returns the release."""
    transport = session._transport
    send, held = transport.broadcast, []

    def broadcast(msg):
        if msg.get("type") == "hand_settled":
            held.append(msg)
        else:
            send(msg)
    transport.broadcast = broadcast

    def release():
        transport.broadcast = send
        for msg in held:
            send(msg)
    return release


def test_the_next_hand_waits_for_every_seats_settlement_report():
    """Seat 2's report is late. Dealing hand 2 before it arrives would let
    seat 2 watch hand 2 and then dispute hand 1, so nobody deals it yet."""
    bus, sessions, order = make_table(3)
    release = _hold_settlement_reports(sessions[order[2]])
    checkdown(bus, sessions, order)
    for cid in order[:2]:
        assert sessions[cid].hand_result is not None
        assert sessions[cid].next_p2p_hand() == "not_ready"
    release()
    bus.drain()
    assert set(next_all(bus, sessions, order).values()) == {"started"}
    assert_synced(sessions, order)


def test_a_settlement_that_differs_ends_the_table_instead_of_looping():
    """One replica pays a chip to the wrong seat -- chips conserved, so the
    conservation guard passes, and after the last action, so no per-action
    digest sees it. Before hand_settled the table only found out in hand 2,
    as a desync whose void redealt from the same disagreeing carry-ins and
    desynced again, indefinitely. Now every seat sees a digest it does not
    share and ends the table at the settlement."""
    bus, sessions, order = make_table(3)
    faulty = sessions[order[1]]
    engine = faulty.replica.engine
    real_settle = engine.settle

    def misdealt_settle(*args, **kwargs):
        out = real_settle(*args, **kwargs)
        engine.players[0].stack -= 1
        engine.players[1].stack += 1
        return out

    engine.settle = misdealt_settle
    while sessions[order[0]].replica.phase == PHASE_BETTING:
        seat = sessions[order[0]].replica.actor
        assert sessions[order[seat]].send_bet_action("call") == "applied"
        bus.drain()

    for cid in order:
        s = sessions[cid]
        assert s.hand_result is not None
        assert s.terminal_state == Session.ABORTED_PROTOCOL, cid
        assert s.terminal_reason.startswith(
            "table state disagrees on hand 1: seat ")
        assert s.next_p2p_hand() == "session_over"
        # Not each peer's own side of the dispute: the stacks the hand was
        # dealt from, which every seat agreed on.
        assert s.last_settled_stacks == [500, 500, 500], cid


def test_a_void_of_a_hand_this_peer_settled_ends_the_table():
    bus, sessions, order = make_table(3)
    checkdown(bus, sessions, order)
    target = sessions[order[0]]
    target.handle_message(order[2], {"type": "hand_void", "hand": 1,
                                     "seat": 2, "reason": "late"})
    assert target.terminal_state == Session.ABORTED_PROTOCOL
    assert target.terminal_reason.startswith(
        "table state disagrees on hand 1: seat 2 voided it, this peer "
        "settled it as ")


def test_a_late_void_of_the_previous_hand_still_ends_the_table(monkeypatch):
    """The void may arrive after this peer has dealt the next hand. Seat 2
    had reported the settlement it now voids, so it is seat 2 that is
    contradicting itself: the table ends, and hand 1 stands."""
    rig_showdowns(monkeypatch)(0, 1, 2)
    bus, sessions, order = make_table(3)
    checkdown(bus, sessions, order)
    target = sessions[order[0]]
    settled = target.replica.stacks
    assert settled == [520, 490, 490]                # seat 0 took 3 x 10
    assert target.next_p2p_hand() == "started"
    assert target._hand_no == 2
    target.handle_message(order[2], {"type": "hand_void", "hand": 1,
                                     "seat": 2, "reason": "late"})
    assert target.terminal_state == Session.ABORTED_PROTOCOL
    assert target.terminal_reason.endswith(", as seat 2 itself had reported")
    assert target.last_settled_stacks == settled


@pytest.mark.parametrize("retraction", [
    {"type": "hand_void", "hand": 1, "reason": "late"},
    {"type": "hand_settled", "hand": 1, "digest": "ab" * 32},
], ids=["void", "other-digest"])
def test_a_seat_cannot_take_back_a_settlement_every_seat_reported(
        monkeypatch, retraction):
    """Seat 2 loses hand 1 and every seat reports the same digest. In hand
    2 seat 2 signs a fresh message disputing hand 1. Rolling back to hand
    1's carry-in would erase a hand the whole table agreed on, and pay
    seat 2 better than leaving does: a disconnect here ends PEER_LOST at
    hand 1's settlement (test_peer_loss). It gets that figure and no
    better, with the blame."""
    rig_showdowns(monkeypatch)(0, 1, 2)
    bus, sessions, order = make_table(3)
    checkdown(bus, sessions, order)
    settled = sessions[order[0]].replica.stacks
    assert settled == [520, 490, 490]                # seat 0 took 3 x 10
    assert all(sessions[c]._ended_agreed == {0, 1, 2} for c in order)
    assert set(next_all(bus, sessions, order).values()) == {"started"}
    act(bus, sessions, order, "call")

    sessions[order[2]]._send_hostless(dict(retraction))
    bus.drain()

    for cid in order[:2]:
        s = sessions[cid]
        assert s.terminal_state == Session.ABORTED_PROTOCOL, cid
        assert s.terminal_record.initiating_seat == 2
        assert s.terminal_reason.startswith(
            "table state disagrees on hand 1: seat 2 ")
        assert s.last_settled_stacks == settled, cid


def test_a_seat_that_settled_a_hand_this_peer_voided_ends_the_table():
    bus, sessions, order = make_table(3)
    target = sessions[order[0]]
    target._void_hand("deal failure", announce=False)
    target.handle_message(order[2], {"type": "hand_settled", "hand": 1,
                                     "seat": 2, "digest": "ab" * 32})
    assert target.terminal_state == Session.ABORTED_PROTOCOL
    assert target.terminal_reason == (
        "table state disagrees on hand 1: seat 2 settled it as "
        "abababababababab, this peer voided it")


def test_an_early_settlement_report_is_held_until_this_peer_ends_the_hand():
    """A faster seat's report can arrive mid-hand here. It is kept, not
    dropped, and judged when this replica ends the hand."""
    bus, sessions, order = make_table(3)
    target = sessions[order[0]]
    target.handle_message(order[2], {"type": "hand_settled", "hand": 1,
                                     "seat": 2, "digest": "cd" * 32})
    assert target.terminal_state is None             # nothing to compare yet
    target._void_hand("deal failure", announce=False)
    assert target.terminal_state == Session.ABORTED_PROTOCOL
    assert "seat 2 settled it as cdcdcdcdcdcdcdcd" in target.terminal_reason


def test_a_settlement_the_table_aborted_over_is_not_announced():
    """An early report disagrees, so this peer's own settle ends the table.
    The client must not then be told the hand settled and paid out."""
    bus, sessions, order = make_table(3)
    target = sessions[order[0]]
    settled = []
    target.on_hand_settled = settled.append
    target.handle_message(order[2], {"type": "hand_settled", "hand": 1,
                                     "seat": 2, "digest": "cd" * 32})
    checkdown(bus, sessions, order)
    assert target.hand_result is not None
    assert target.terminal_state == Session.ABORTED_PROTOCOL
    assert settled == []
    assert sessions[order[1]].terminal_state is None   # it never saw the lie


def test_a_seat_not_dealt_in_cannot_dispute_the_settlement():
    """Seat 2 holds no chips, so no hand deals it. Ingress still admits
    what it signs -- it does own seat 2 -- but it settled and voided
    nothing, so no report of its can end the table: not one held from
    before the settle, not one after it, and not a late void."""
    bus, sessions, order = make_table(3, stacks=[500, 500, 0])
    live = [sessions[order[0]], sessions[order[1]]]
    assert live[0].replica.seats_dealt == [0, 1]
    bogus = {"type": "hand_settled", "hand": 1, "seat": 2,
             "digest": "ee" * 32}
    for s in live:
        s.handle_message(order[2], dict(bogus))      # held until the settle?
    checkdown(bus, sessions, order, alive=[0, 1])
    for s in live:
        assert s.hand_result is not None
        assert s.terminal_state is None, s.terminal_reason
        s.handle_message(order[2], dict(bogus))      # compared after it?
        assert s.terminal_state is None, s.terminal_reason
        s.handle_message(order[2], {"type": "hand_void", "hand": 1,
                                    "seat": 2, "reason": "late"})
        assert s.terminal_state is None, s.terminal_reason


def test_a_matching_report_after_settling_changes_nothing():
    bus, sessions, order = make_table(3)
    checkdown(bus, sessions, order)
    target = sessions[order[0]]
    target.handle_message(order[2], {
        "type": "hand_settled", "hand": 1, "seat": 2,
        "digest": target.replica.state_digest()})
    assert target.terminal_state is None
    assert set(next_all(bus, sessions, order).values()) == {"started"}
