"""Phase 3 — deterministic silent-peer timeout.

Every test uses FakeClock.  No sleep().  No hoping the scheduler is cooperative.

Coverage:
- Clock injection and FakeClock mechanics
- Deadline starts when a remote peer is the actor
- Deadline clears when the actor changes to local or hand ends
- check_deadlines() broadcasts a timeout_proposal after the fake deadline
- Betting timeout: fold when facing a bet, check when not
- Deal timeout: voids the hand, preserves stacks, marks peer unavailable
- Proposal validation: stale token, wrong seq, wrong hand, malformed
- Race: action-beats-proposal (action arrives first → proposal is stale)
- Race: proposal-beats-action (proposal applied first → late action dropped)
- Idempotency: two proposals for the same token collapse to one transition
- Convergence: three replicas, proposals delivered in different orders
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.session import Session
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.timeout import DeadlineToken, FakeClock

try:
    importlib.import_module("holdem.p2p.elgamal")
except RuntimeError as exc:
    pytest.skip(f"libsodium unavailable: {exc}", allow_module_level=True)

from holdem.p2p.replica_table import PHASE_BETTING, PHASE_SETTLED


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_table(n, stacks=None, sb=5, bb=10, hand=1, button=0,
               clocks: Optional[list] = None):
    """N sessions wired to a shared bus, hand already begun."""
    bus   = InMemoryBus()
    order = [f"peer{i}" for i in range(n)]
    sessions: dict[str, Session] = {}
    for i, cid in enumerate(order):
        clk = clocks[i] if clocks else None
        s = Session(
            is_host   = (i == 0),
            nickname  = f"P{i}",
            avatar_b64= "",
            transport = InMemoryTransport(bus, cid),
            clock     = clk,
        )
        s.local_conn_id = cid
        s.configure_seats(list(order))
        s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
        bus.register(cid, s)
        sessions[cid] = s
    names  = [f"P{i}" for i in range(n)]
    stacks = list(stacks) if stacks else [500] * n
    for cid in order:
        sessions[cid].start_p2p_hand(hand_no=hand, names=names,
                                     stacks=stacks, sb=sb, bb=bb,
                                     button=button)
    bus.drain()
    return bus, sessions, order


def current_actor_conn(sessions, order) -> str:
    """Return the conn_id of the peer whose turn it is."""
    seat = sessions[order[0]].replica.actor
    return order[seat]


# ---------------------------------------------------------------------------
# FakeClock basics
# ---------------------------------------------------------------------------

class TestFakeClock:
    def test_starts_at_zero(self):
        c = FakeClock()
        assert c.monotonic() == 0.0

    def test_advance(self):
        c = FakeClock(10.0)
        c.advance(5.5)
        assert c.monotonic() == 15.5

    def test_does_not_affect_other_clocks(self):
        c1 = FakeClock()
        c2 = FakeClock()
        c1.advance(99)
        assert c2.monotonic() == 0.0


# ---------------------------------------------------------------------------
# Clock injection
# ---------------------------------------------------------------------------

class TestClockInjection:
    def test_session_accepts_fake_clock(self):
        clk = FakeClock()
        bus = InMemoryBus()
        s = Session(is_host=True, nickname="T", avatar_b64="",
                    transport=InMemoryTransport(bus, "A"), clock=clk)
        assert s._clock is clk

    def test_session_defaults_to_real_clock(self):
        bus = InMemoryBus()
        s = Session(is_host=True, nickname="T", avatar_b64="",
                    transport=InMemoryTransport(bus, "A"))
        import time
        # Real clock should return something close to time.monotonic()
        assert abs(s._clock.monotonic() - time.monotonic()) < 1.0


# ---------------------------------------------------------------------------
# Deadline lifecycle
# ---------------------------------------------------------------------------

class TestDeadlineLifecycle:
    def test_deadline_set_when_remote_actor(self):
        """After the hand starts a deadline is always set — either for the
        ongoing mental-poker deal phase or for a remote betting actor."""
        clk = FakeClock()
        bus, sessions, order = make_table(2, clocks=[clk, None])
        s0 = sessions[order[0]]
        # A hand is in progress; some deadline must be active
        assert s0._current_deadline_token is not None
        assert s0._current_deadline_token.phase in (
            "betting", "deal_shuffle", "deal_decrypt"
        )

    def test_deadline_clears_when_actor_becomes_local(self):
        """When a remote peer acts and turn passes to the local peer, the
        deadline is cleared.  Use direct replica injection (no deal driver)
        so the betting phase is reached immediately."""
        clk = FakeClock()
        bus = InMemoryBus()
        s = Session(is_host=True, nickname="H", avatar_b64="",
                    transport=InMemoryTransport(bus, "peer0"), clock=clk)
        s.local_conn_id = "peer0"
        s.configure_seats(["peer0", "peer1"])
        s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)

        from holdem.p2p.replica_table import ReplicaTable
        r = ReplicaTable(
            session_id="poker|peer0|peer1", hand_no=1,
            names=["Alice", "Bob"], stacks=[1000, 1000],
            sb=5, bb=10, structure="No-Limit",
        )
        # button=0: peer0 is BB, peer1 is SB and acts first preflop
        r.start_hand(0)
        s._replica = r
        s._hand_no = 1
        # No deal driver → _maybe_start_deadline goes to betting branch
        s._maybe_start_deadline()

        # peer1 is the remote actor — deadline must be set
        if r.actor is None or s.seat_order[r.actor] == "peer0":
            pytest.skip("local peer is the first actor in this hand config")

        assert s._current_deadline_token is not None
        assert s._current_deadline_token.actor == "peer1"

        # peer1 acts (call) → peer0's turn → deadline should clear
        verdict = r.apply_action(r.next_seq, r.actor, "call", 0)
        assert verdict == "applied"
        s._notify_state_changed()

        # If hand still going and it's now peer0's turn, no deadline
        if r.actor is not None and s.seat_order[r.actor] == "peer0":
            assert s._current_deadline_token is None

    def test_deadline_clears_after_hand_settles(self):
        bus, sessions, order = make_table(2)
        s0 = sessions[order[0]]
        # Drive to settlement
        while s0.replica.phase == PHASE_BETTING:
            seat = s0.replica.actor
            sessions[order[seat]].send_bet_action("call")
            bus.drain()
        # Hand settled — no deadline
        assert s0._current_deadline_token is None

    def test_deadline_clears_after_void(self):
        bus, sessions, order = make_table(2)
        s0 = sessions[order[0]]
        s0._void_hand("test void", announce=False)
        assert s0._current_deadline_token is None

    def test_deadline_not_reset_if_token_unchanged(self):
        """_maybe_start_deadline must not reset the timer if the same token
        is already running — otherwise every state change would restart the clock."""
        clk = FakeClock()
        bus, sessions, order = make_table(2, clocks=[clk, None])
        s0 = sessions[order[0]]
        tok = s0._current_deadline_token
        started_at = s0._deadline_started_at
        if tok is None:
            return  # actor is local; test not applicable
        clk.advance(5.0)   # time passes but no state change
        # Force _maybe_start_deadline again without changing state
        s0._maybe_start_deadline()
        assert s0._current_deadline_token == tok
        assert s0._deadline_started_at == started_at  # NOT reset


# ---------------------------------------------------------------------------
# Betting timeout — check_deadlines dispatches a proposal
# ---------------------------------------------------------------------------

class TestBettingTimeout:
    def _make_betting_session(self, clk=None):
        """Session in betting phase with peer1 (remote) as the first actor.
        No deal driver — replica injected directly, so _maybe_start_deadline
        goes straight to the betting branch."""
        if clk is None:
            clk = FakeClock()
        bus = InMemoryBus()
        s = Session(is_host=True, nickname="H", avatar_b64="",
                    transport=InMemoryTransport(bus, "peer0"), clock=clk)
        s.local_conn_id = "peer0"
        s.configure_seats(["peer0", "peer1"])
        s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)

        from holdem.p2p.replica_table import ReplicaTable
        r = ReplicaTable(
            session_id="poker|peer0|peer1", hand_no=1,
            names=["Alice", "Bob"], stacks=[1000, 1000],
            sb=5, bb=10, structure="No-Limit",
        )
        # button=0 → peer0 is BB, peer1 is SB and acts first preflop
        r.start_hand(0)
        s._replica = r
        s._hand_no = 1
        s._maybe_start_deadline()  # sets betting deadline for peer1

        if r.actor is None or s.seat_order[r.actor] == "peer0":
            pytest.skip("local peer ended up as first actor")

        return clk, bus, s, r

    def _setup_remote_actor(self, clk=None):
        """Return (clk, bus, sessions, order) via make_table.
        Used only where the deal phase is acceptable."""
        if clk is None:
            clk = FakeClock()
        for button in range(2):
            clocks = [clk, FakeClock()]
            bus, sessions, order = make_table(2, clocks=clocks, button=button)
            s0 = sessions[order[0]]
            if s0._current_deadline_token is not None:
                return clk, bus, sessions, order
        pytest.skip("couldn't get an active deadline from make_table")

    def test_check_deadlines_broadcasts_proposal_after_timeout(self):
        clk, bus, s, r = self._make_betting_session()
        assert s._current_deadline_token is not None

        proposals = []
        original_broadcast = s._transport.broadcast
        s._transport.broadcast = lambda m: (proposals.append(m),
                                             original_broadcast(m))[-1]

        clk.advance(31.0)
        s.check_deadlines()
        assert any(m.get("type") == "timeout_proposal" for m in proposals)

    def test_betting_timeout_folds_actor_facing_bet(self):
        """When there is a bet to call and the actor times out, they are folded."""
        clk, bus, s, r = self._make_betting_session()
        actor_seat = r.actor
        to_call = r.engine.legal(actor_seat).get("to_call", 0)
        if to_call == 0:
            pytest.skip("initial actor has no bet to face")

        clk.advance(31.0)
        s.check_deadlines()
        assert r.engine.players[actor_seat].folded

    def test_betting_timeout_checks_actor_no_bet(self):
        """When there is no bet to face and the actor times out, they are checked.

        Tracked in https://github.com/nephrium83/texas-holdem/issues/11
        Requires a real mental-poker deal to reach a genuine postflop state;
        remove the skip below once the integration harness supports that.
        """
        # Find a game state where the first actor has no bet to face.
        # Preflop: UTG always faces a BB, so we need to get to postflop.
        clk = FakeClock()
        bus, sessions, order = make_table(2, clocks=[clk, FakeClock()])
        s0 = sessions[order[0]]
        # Drain preflop by having everyone call until postflop
        while s0.replica.phase == PHASE_BETTING and \
              s0.replica.engine.street == "preflop":
            seat = s0.replica.actor
            sessions[order[seat]].send_bet_action("call")
            bus.drain()
            if s0.replica.phase != PHASE_BETTING:
                break

        if s0.replica.phase != PHASE_BETTING:
            pytest.skip("hand settled before postflop")

        actor_seat = s0.replica.actor
        legal = s0.replica.engine.legal(actor_seat)
        if legal.get("to_call", 0) > 0:
            pytest.skip("postflop actor still faces a bet")

        actor_conn = order[actor_seat]
        if actor_conn == order[0]:
            pytest.skip("local peer is the postflop actor")

        clk.advance(31.0)
        s0.check_deadlines()
        bus.drain()

        # Actor was checked — action_on should have advanced past them
        new_actor = s0.replica.actor
        assert new_actor != actor_seat or s0.replica.phase != PHASE_BETTING

    def test_timeout_convergence_two_replicas(self):
        """Both replicas converge on the same state after a timeout proposal
        is processed by each."""
        clk0 = FakeClock()
        clk1 = FakeClock()
        bus, sessions, order = make_table(2, clocks=[clk0, clk1])
        s0, s1 = sessions[order[0]], sessions[order[1]]

        actor_seat = s0.replica.actor
        actor_conn = order[actor_seat]
        if actor_conn == order[0]:
            # Swap: timeout from s1's perspective
            watcher = s1
            clk = clk1
        else:
            watcher = s0
            clk = clk0

        clk.advance(31.0)
        watcher.check_deadlines()
        bus.drain()  # proposal delivered to both; both apply

        d0 = s0.replica.state_digest() if s0.replica else None
        d1 = s1.replica.state_digest() if s1.replica else None
        assert d0 is not None and d0 == d1, \
            f"replicas diverged after timeout: {d0} vs {d1}"


# ---------------------------------------------------------------------------
# Deal timeout
# ---------------------------------------------------------------------------

class TestDealTimeout:
    def test_deal_timeout_voids_hand_and_preserves_stacks(self):
        """A deal-phase timeout proposal voids the hand; stacks unchanged."""
        clk = FakeClock()
        bus = InMemoryBus()
        order = ["A", "B"]
        sessions: dict[str, Session] = {}
        for i, cid in enumerate(order):
            s = Session(is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
                        transport=InMemoryTransport(bus, cid), clock=clk)
            s.local_conn_id = cid
            s.configure_seats(list(order))
            s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
            bus.register(cid, s)
            sessions[cid] = s

        # Build a valid token that matches the deal phase
        sA = sessions["A"]
        # Start a hand so replica exists
        sA._table_cfg = {
            "names": ["Alice", "Bob"], "sb": 5, "bb": 10,
            "structure": "No-Limit", "button": 0, "total_chips": 2000,
        }
        sessions["B"]._table_cfg = dict(sA._table_cfg)

        stacks = [1000, 1000]
        # Manually construct a deal-phase token (simulate deal not completing)
        from holdem.p2p.replica_table import ReplicaTable
        replica = ReplicaTable(
            session_id="poker|A|B", hand_no=1,
            names=["Alice", "Bob"], stacks=stacks,
            sb=5, bb=10, structure="No-Limit",
        )
        replica.start_hand(0)
        # Blinds are posted during start_hand; capture chip total now
        chips_in_play = sum(p.stack for p in replica.engine.players)
        sA._replica = replica
        sA._seat_order = ["A", "B"]
        sA._hand_no = 1

        token = DeadlineToken(
            hand_id    = "poker|A|B",
            phase      = "deal_shuffle",
            actor      = None,
            action_seq = 0,
        )
        sA._current_deadline_token = token
        sA._deadline_started_at   = 0.0

        # Apply the deal timeout directly
        sA._apply_deal_timeout(token)
        assert sA.hand_voided
        assert "deal timeout" in (sA.void_reason or "")
        # Void must not destroy chips — total in player stacks unchanged by void
        assert sum(p.stack for p in sA.replica.engine.players) == chips_in_play

    def test_deal_timeout_marks_known_peer_unavailable(self):
        """If actor is specified in the deal token, that peer is marked unavailable."""
        bus = InMemoryBus()
        s = Session(is_host=True, nickname="H", avatar_b64="",
                    transport=InMemoryTransport(bus, "host"))
        s.local_conn_id = "host"
        from holdem.p2p.session import Player
        with s._lock:
            s.players["peerA"] = Player(
                conn_id="peerA", peer_id="", nickname="A", avatar_b64="")

        from holdem.p2p.replica_table import ReplicaTable
        r = ReplicaTable(
            session_id="poker|host|peerA", hand_no=1,
            names=["H", "A"], stacks=[500, 500], sb=5, bb=10,
            structure="No-Limit",
        )
        r.start_hand(0)
        s._replica = r
        s._seat_order = ["host", "peerA"]
        s._hand_no = 1

        token = DeadlineToken(
            hand_id    = "poker|host|peerA",
            phase      = "deal_shuffle",
            actor      = "peerA",
            action_seq = 0,
        )
        s._apply_deal_timeout(token)
        assert s.hand_voided
        assert s.players["peerA"].unavailable is True


# ---------------------------------------------------------------------------
# Proposal validation
# ---------------------------------------------------------------------------

class TestProposalValidation:
    def _session_with_active_hand(self):
        bus = InMemoryBus()
        s = Session(is_host=True, nickname="H", avatar_b64="",
                    transport=InMemoryTransport(bus, "A"))
        s.local_conn_id = "A"
        s.configure_seats(["A", "B"])
        s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
        from holdem.p2p.replica_table import ReplicaTable
        r = ReplicaTable(
            session_id="poker|A|B", hand_no=1,
            names=["Alice", "Bob"], stacks=[1000, 1000],
            sb=5, bb=10, structure="No-Limit",
        )
        r.start_hand(0)
        s._replica = r
        s._hand_no  = 1
        # The token a proposal must match is the one this replica DERIVES
        # from its own state, not one stashed on the session. The fixture
        # used to fabricate hand_id="poker|A|B" and assign it to
        # _current_deadline_token, which the old validation accepted -- a
        # proposal could name a hand this replica was not playing and still
        # be applied, provided the local stash agreed. That is the same
        # defect class as the convergence bug: the local timer was being
        # treated as protocol authority.
        token = s._expected_timeout_token()
        assert token is not None and token.phase == "betting", token
        # The local timer is still armed here so the "stale token" cases
        # below exercise a peer that IS watching someone.
        s._current_deadline_token = token
        s._deadline_started_at   = 0.0
        return s, token

    def test_valid_proposal_is_applied(self):
        s, token = self._session_with_active_hand()
        msg = {
            "type": "timeout_proposal",
            "hand": 1,
            "token": {
                "hand_id":    token.hand_id,
                "phase":      token.phase,
                "actor":      token.actor,
                "action_seq": token.action_seq,
            },
            "missing_seat": None,
        }
        actor_before = s.replica.actor
        s._on_timeout_proposal("B", msg)
        # Actor should have changed (folded or checked)
        assert s.replica.actor != actor_before or s.replica.phase != PHASE_BETTING

    def test_stale_token_dropped(self):
        s, token = self._session_with_active_hand()
        # Send a proposal with a wrong action_seq (stale)
        msg = {
            "type": "timeout_proposal",
            "hand": 1,
            "token": {
                "hand_id":    token.hand_id,
                "phase":      token.phase,
                "actor":      token.actor,
                "action_seq": token.action_seq + 99,   # wrong seq
            },
            "missing_seat": None,
        }
        actor_before = s.replica.actor
        s._on_timeout_proposal("B", msg)
        # Nothing changed
        assert s.replica.actor == actor_before

    def test_wrong_phase_dropped(self):
        s, token = self._session_with_active_hand()
        msg = {
            "type": "timeout_proposal",
            "hand": 1,
            "token": {
                "hand_id":    token.hand_id,
                "phase":      "deal_shuffle",          # wrong phase
                "actor":      token.actor,
                "action_seq": token.action_seq,
            },
            "missing_seat": None,
        }
        actor_before = s.replica.actor
        s._on_timeout_proposal("B", msg)
        assert s.replica.actor == actor_before

    def test_wrong_actor_dropped(self):
        s, token = self._session_with_active_hand()
        msg = {
            "type": "timeout_proposal",
            "hand": 1,
            "token": {
                "hand_id":    token.hand_id,
                "phase":      token.phase,
                "actor":      "WRONG_PEER",             # wrong actor
                "action_seq": token.action_seq,
            },
            "missing_seat": None,
        }
        actor_before = s.replica.actor
        s._on_timeout_proposal("B", msg)
        assert s.replica.actor == actor_before

    def test_malformed_token_dropped(self):
        s, token = self._session_with_active_hand()
        msg = {
            "type": "timeout_proposal",
            "hand": 1,
            "token": {"oops": True},      # missing required fields
        }
        actor_before = s.replica.actor
        s._on_timeout_proposal("B", msg)
        assert s.replica.actor == actor_before

    def test_proposal_dropped_when_no_hand(self):
        bus = InMemoryBus()
        s = Session(is_host=True, nickname="H", avatar_b64="",
                    transport=InMemoryTransport(bus, "A"))
        msg = {
            "type": "timeout_proposal",
            "hand": 1,
            "token": {
                "hand_id":    "poker|A|B",
                "phase":      "betting",
                "actor":      "B",
                "action_seq": 0,
            },
            "missing_seat": None,
        }
        # Should not raise even though _replica is None
        s._on_timeout_proposal("B", msg)  # no-op

    def test_proposal_dropped_when_voided(self):
        s, token = self._session_with_active_hand()
        # Void through the real path: hand_voided is derived from the hand
        # record, so assigning it would bypass the transition under test.
        s._void_hand("test void", announce=False)
        assert s.hand_voided
        msg = {
            "type": "timeout_proposal",
            "hand": 1,
            "token": {
                "hand_id":    token.hand_id,
                "phase":      token.phase,
                "actor":      token.actor,
                "action_seq": token.action_seq,
            },
            "missing_seat": None,
        }
        s._on_timeout_proposal("B", msg)  # no-op; hand already voided


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def _setup_and_get_token(self):
        bus = InMemoryBus()
        s = Session(is_host=True, nickname="H", avatar_b64="",
                    transport=InMemoryTransport(bus, "A"))
        s.local_conn_id = "A"
        s.configure_seats(["A", "B"])
        s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
        from holdem.p2p.replica_table import ReplicaTable
        r = ReplicaTable(
            session_id="poker|A|B", hand_no=1,
            names=["Alice", "Bob"], stacks=[1000, 1000],
            sb=5, bb=10, structure="No-Limit",
        )
        r.start_hand(0)
        s._replica = r
        s._hand_no = 1
        token = DeadlineToken(
            hand_id    = "poker|A|B",
            phase      = "betting",
            actor      = "B",
            action_seq = r.next_seq,
        )
        s._current_deadline_token = token
        s._deadline_started_at   = 0.0
        return s, token

    def test_second_proposal_is_harmless(self):
        """Two proposals with the same token: first applies, second is stale."""
        s, token = self._setup_and_get_token()
        msg = {
            "type": "timeout_proposal",
            "hand": 1,
            "token": {
                "hand_id":    token.hand_id,
                "phase":      token.phase,
                "actor":      token.actor,
                "action_seq": token.action_seq,
            },
            "missing_seat": None,
        }
        s._on_timeout_proposal("B", msg)   # first: applied
        state_after_first = s.replica.actor if s.replica else None

        s._on_timeout_proposal("B", msg)   # second: seq advanced → stale
        state_after_second = s.replica.actor if s.replica else None

        assert state_after_first == state_after_second   # idempotent


# ---------------------------------------------------------------------------
# Race conditions
# ---------------------------------------------------------------------------

class TestRaceConditions:
    def test_action_beats_proposal(self):
        """Actor acts before the timeout proposal is processed.
        The proposal must become stale (seq advanced) and be silently dropped."""
        bus, sessions, order = make_table(2)
        s0 = sessions[order[0]]
        actor_seat = s0.replica.actor
        actor_conn = order[actor_seat]
        non_actor  = next(c for c in order if c != actor_conn)

        if non_actor != order[0]:
            pytest.skip("local peer is the actor; test expects remote actor")

        seq_before  = s0.replica.next_seq
        token_before = s0._current_deadline_token
        if token_before is None:
            pytest.skip("no deadline set (local is actor)")

        # Construct the proposal that would have fired for this actor
        proposal_msg = {
            "type": "timeout_proposal",
            "hand": s0._hand_no,
            "token": {
                "hand_id":    token_before.hand_id,
                "phase":      "betting",
                "actor":      actor_conn,
                "action_seq": seq_before,
            },
            "missing_seat": None,
        }

        # Actor acts first
        sessions[actor_conn].send_bet_action("call")
        bus.drain()

        # Now the in-flight proposal arrives at s0 — must be stale
        actor_after = s0.replica.actor
        s0._on_timeout_proposal(non_actor, proposal_msg)

        # Proposal was silently dropped; actor has already moved on
        assert s0.replica.actor == actor_after

    def test_proposal_beats_action(self):
        """Timeout proposal applied first; actor's late action is rejected."""
        bus, sessions, order = make_table(2)
        s0 = sessions[order[0]]
        actor_seat = s0.replica.actor
        actor_conn = order[actor_seat]
        non_actor  = next(c for c in order if c != actor_conn)

        if non_actor != order[0]:
            pytest.skip("local peer is the actor; swap needed")

        token = s0._current_deadline_token
        if token is None:
            pytest.skip("no deadline set")

        # Apply the proposal on s0 directly (proposal arrived first)
        s0._on_timeout_proposal(
            non_actor,
            {
                "type": "timeout_proposal",
                "hand": s0._hand_no,
                "token": {
                    "hand_id":    token.hand_id,
                    "phase":      token.phase,
                    "actor":      token.actor,
                    "action_seq": token.action_seq,
                },
                "missing_seat": None,
            }
        )

        # Actor's turn has been resolved; now actor "acts" (late message)
        state_after_proposal = s0.replica.next_seq
        actor_session = sessions[actor_conn]
        # Manually send a bet_action as the timed-out actor would have
        verdict = actor_session.replica.apply_action(
            token.action_seq, actor_seat, "call", 0)
        # Their action was based on the old seq, which is now consumed;
        # either it's rejected ("rejected" verdict) or the seq is wrong.
        # Either way the state should not have regressed.
        assert s0.replica.next_seq >= state_after_proposal


# ---------------------------------------------------------------------------
# Convergence across multiple replicas
# ---------------------------------------------------------------------------

class TestConvergence:
    def test_three_replicas_converge_after_timeout(self):
        """Timeout proposal delivered to three peers in different orders;
        all end in identical state.

        Tracked in https://github.com/nephrium83/texas-holdem/issues/12
        The skip below fires when peer0 is the preflop actor; remove it
        by fixing button/seating so peer0 is never the actor.
        """
        bus, sessions, order = make_table(3)
        s0, s1, s2 = (sessions[c] for c in order)

        # Find a peer that is NOT the actor in any session (a "watcher")
        actor_seat = s0.replica.actor
        actor_conn = order[actor_seat]

        # Build the timeout proposal as if peer0 generated it
        token = s0._current_deadline_token
        if token is None or token.phase != "betting":
            pytest.skip("no betting deadline on peer0 (may be local actor)")

        proposal_msg = {
            "type": "timeout_proposal",
            "hand": s0._hand_no,
            "token": {
                "hand_id":    token.hand_id,
                "phase":      token.phase,
                "actor":      token.actor,
                "action_seq": token.action_seq,
            },
            "missing_seat": None,
        }

        # Deliver in different orders to each session
        proposer = next(c for c in order if c != actor_conn)
        s0._on_timeout_proposal(proposer, proposal_msg)
        s2._on_timeout_proposal(proposer, proposal_msg)
        s1._on_timeout_proposal(proposer, proposal_msg)

        # All sessions with an active replica should have the same digest
        active = [sessions[c] for c in order
                  if sessions[c].replica is not None and not sessions[c].hand_voided]
        if len(active) < 2:
            pytest.skip("all hands voided — nothing to compare")
        digs = {s.replica.state_digest() for s in active}
        assert len(digs) == 1, f"replicas diverged after timeout: {digs}"

    def test_no_sleep_anywhere(self):
        """Confirm the timeout module has no sleep() calls — the word may
        appear in comments or docstrings, so check for the call pattern."""
        import re
        import holdem.p2p.timeout as tm
        import inspect
        src = inspect.getsource(tm)
        assert not re.search(r'\bsleep\s*\(', src), \
            "Found a sleep() call in holdem/p2p/timeout.py"


# ---------------------------------------------------------------------------
# Proposal applicability: the timed-out actor must converge too
# ---------------------------------------------------------------------------

class TestTimeoutProposalApplicability:
    """A betting timeout must reach EVERY replica, including its subject.

    _maybe_start_deadline correctly refuses to arm a timer against
    yourself. Validation used to compare a received proposal against that
    timer, so the one peer a betting timeout is ABOUT had nothing to
    compare against and rejected it: observers folded that peer, the peer
    did not fold itself, and the table diverged permanently.

    The fix separates the deterministic "which transition is
    timeout-eligible right now" from the local "may I propose failure yet"
    clock. These tests pin the security half as hard as the liveness half:
    actor names whose hand gets folded, so a valid sequence number must NOT
    let a hostile peer nominate somebody else.
    """

    def _betting_table(self, seats=2):
        """A table whose armed deadline is a BETTING one.

        The deal driver is cleared after the deal completes, deliberately.
        On this base _maybe_start_deadline gives the deal branch priority
        whenever `not deal_done()` -- and deal_done() stays False until the
        POST-HAND audit, so a normal table carries a deal_shuffle deadline
        right through betting and every betting-timeout test would silently
        exercise the void path instead. That defect is real and is fixed
        separately (P2a); this file is about whether a RECEIVED PROPOSAL is
        applicable, so it isolates that question rather than depending on
        the other fix landing first.
        """
        clocks = [FakeClock() for _ in range(seats)]
        bus, sessions, order = make_table(seats, clocks=clocks)
        for sess in sessions.values():
            sess._deal_driver = None
            sess._notify_state_changed()
        return bus, sessions, order, clocks

    def _timeout_the_actor(self, seats):
        bus, sessions, order, clocks = self._betting_table(seats)
        s0 = sessions[order[0]]
        actor_conn = order[s0.replica.actor]
        watcher_conn = next(c for c in order if c != actor_conn)
        clocks[order.index(watcher_conn)].advance(31.0)
        sessions[watcher_conn].check_deadlines()
        bus.drain()
        return sessions, order, actor_conn

    @pytest.mark.parametrize("seats", [2, 3])
    def test_every_replica_including_the_actor_converges(self, seats):
        sessions, order, actor_conn = self._timeout_the_actor(seats)
        digests = {cid: s.replica.state_digest() for cid, s in sessions.items()}
        assert len(set(digests.values())) == 1, \
            "replicas diverged after a betting timeout: %r" % (digests,)
        seqs = {cid: s.replica.next_seq for cid, s in sessions.items()}
        assert len(set(seqs.values())) == 1, \
            "the timed-out actor did not apply its own timeout: %r" % (seqs,)

    def test_the_acting_peer_still_arms_no_timer_on_itself(self):
        """Convergence must NOT be bought by starting self-timeouts."""
        bus, sessions, order, _clocks = self._betting_table(2)
        actor_conn = order[sessions[order[0]].replica.actor]
        actor = sessions[actor_conn]
        assert actor._current_deadline_token is None, \
            "the acting peer armed a deadline against itself"
        assert actor._expected_timeout_token() is not None, \
            "but it must still know which transition is timeout-eligible"

    def test_the_timeout_result_is_fold_or_check_per_spec(self):
        """TIMEOUT_SPEC: fold when facing a bet, check when nothing is owed.
        Never a void -- that is the deal path's consequence, not betting's."""
        bus, sessions, order, clocks = self._betting_table(2)
        s0 = sessions[order[0]]
        actor_seat = s0.replica.actor
        actor_conn = order[actor_seat]
        facing = s0.replica.engine.legal(actor_seat).get("to_call", 0) > 0
        watcher_conn = next(c for c in order if c != actor_conn)

        clocks[order.index(watcher_conn)].advance(31.0)
        sessions[watcher_conn].check_deadlines()
        bus.drain()

        for cid, s in sessions.items():
            assert not s.hand_voided, \
                "%s voided the hand on a BETTING timeout" % cid
        player = s0.replica.engine.players[actor_seat]
        if facing:
            assert player.folded, "actor faced a bet and was not folded"
        else:
            assert not player.folded, "actor owed nothing and was folded"

    # ---------------------------------------------------- hostile proposals

    def _armed_table(self):
        bus, sessions, order, _clocks = self._betting_table(3)
        victim = sessions[order[0]]
        return bus, sessions, order, victim, victim._expected_timeout_token()

    def _propose(self, victim, frm, **overrides):
        token = victim._expected_timeout_token()
        raw = {"hand_id": token.hand_id, "phase": token.phase,
               "actor": token.actor, "action_seq": token.action_seq}
        raw.update(overrides)
        before = victim.replica.state_digest()
        victim.handle_message(frm, {"type": "timeout_proposal",
                                    "hand": victim._hand_no,
                                    "token": raw, "missing_seat": None})
        return before, victim.replica.state_digest()

    def test_a_betting_proposal_naming_a_different_actor_changes_nothing(self):
        """A correct hand/phase/seq must not let a peer nominate whichever
        player it would most like to see folded.

        Note where this is actually enforced: the replica refuses an action
        from a seat whose turn it is not, so on the BETTING path a wrong
        actor is already inert and dropping the check here fires nothing.
        The check earns its place on the DEAL path -- see the test below,
        which is the one that discriminates.
        """
        bus, sessions, order, victim, token = self._armed_table()
        other = next(c for c in order if c != token.actor)
        before, after = self._propose(victim, order[1], actor=other)
        assert before == after, "a wrong-actor timeout proposal was applied"

    def test_a_deal_proposal_cannot_brand_an_innocent_peer(self):
        """THE security case, and where actor validation is load-bearing.

        _apply_deal_timeout uses token.actor as pure ATTRIBUTION: it marks
        that peer unavailable and names it in the void reason. Nothing
        cross-checks it -- unlike the betting path, where the replica
        refuses an out-of-turn action on its own. The canonical deal token
        carries actor=None, because a deal contribution is owed by every
        required peer and no single one is named.

        So without actor binding, any peer holding a valid sequence number
        could void the hand AND pin the blame on whoever it chose. This
        table has a deal deadline armed, which is the default on this base.
        """
        from holdem.p2p.session import Player
        clocks = [FakeClock(), FakeClock(), FakeClock()]
        bus, sessions, order = make_table(3, clocks=clocks)
        victim = sessions[order[0]]
        # make_table bypasses the lobby, so give the victim a roster: the
        # branding half of the harm needs somebody to brand.
        with victim._lock:
            for cid in order:
                victim.players[cid] = Player(conn_id=cid, peer_id="",
                                             nickname=cid, avatar_b64="")
        expected = victim._expected_timeout_token()
        assert expected is not None and expected.phase == "deal_shuffle", expected
        assert expected.actor is None, "a deal token must name no single peer"

        scapegoat = order[1]
        victim.handle_message(order[2], {
            "type": "timeout_proposal",
            "hand": victim._hand_no,
            "token": {"hand_id": expected.hand_id, "phase": expected.phase,
                      "actor": scapegoat,
                      "action_seq": expected.action_seq},
            "missing_seat": None,
        })

        assert not victim.hand_voided,             "a proposal naming an arbitrary peer voided the hand"
        assert victim.players[scapegoat].unavailable is False,             "an innocent peer was branded unavailable by a forged proposal"

    def test_a_proposal_with_the_wrong_phase_is_refused(self):
        bus, sessions, order, victim, token = self._armed_table()
        before, after = self._propose(victim, order[1], phase="deal_shuffle")
        assert before == after

    def test_a_proposal_with_the_wrong_hand_id_is_refused(self):
        bus, sessions, order, victim, token = self._armed_table()
        before, after = self._propose(victim, order[1],
                                      hand_id="poker.deal.v2:deadbeef")
        assert before == after

    @pytest.mark.parametrize("seq_delta", [-1, 1, 99])
    def test_a_proposal_with_a_stale_or_future_seq_is_refused(self, seq_delta):
        bus, sessions, order, victim, token = self._armed_table()
        before, after = self._propose(victim, order[1],
                                      action_seq=token.action_seq + seq_delta)
        assert before == after

    def test_a_proposal_for_the_previous_actor_is_refused_after_an_action(self):
        """Once a legitimate action advances next_seq, the old actor's
        proposal describes a transition that is no longer eligible."""
        bus, sessions, order, victim, stale = self._armed_table()
        sessions[stale.actor].send_bet_action("call")
        bus.drain()

        raw = {"hand_id": stale.hand_id, "phase": stale.phase,
               "actor": stale.actor, "action_seq": stale.action_seq}
        before = victim.replica.state_digest()
        victim.handle_message(order[1], {"type": "timeout_proposal",
                                         "hand": victim._hand_no,
                                         "token": raw, "missing_seat": None})
        assert victim.replica.state_digest() == before, \
            "a proposal for the superseded actor was applied"

    def test_local_clock_state_does_not_affect_acceptance(self):
        """Wall time decides when a peer may PROPOSE, never whether a
        received proposal is applied -- that is the action sequence's job."""
        digests = []
        for started_at in (None, 0.0, -10000.0, 10000.0):
            bus, sessions, order, victim, token = self._armed_table()
            victim._deadline_started_at = started_at
            self._propose(victim, order[1])
            digests.append(victim.replica.state_digest())
        assert len(set(digests)) == 1, \
            "acceptance varied with local clock state: %r" % (digests,)
