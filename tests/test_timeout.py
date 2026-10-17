"""Phase 3 — deterministic silent-peer timeout.

Every test uses FakeClock.  No sleep().  No hoping the scheduler is cooperative.

Coverage:
- Clock injection and FakeClock mechanics
- Deadline starts when a remote peer is the actor
- Deadline clears when the actor changes to local or hand ends
- check_deadlines() reports an expired deadline once, as a timeout_proposal,
  and applies nothing itself
- Betting timeout consequence: fold when facing a bet, check when not
- Deal timeout consequence: voids the hand, preserves stacks, marks peer
  unavailable
- Refusal: no game applies a timeout proposal -- current or stale, at time
  zero or after its own deadline (docs/CASUAL_P2P_RULES.md, "Time-zero
  timeout proposal")
- Race: action-beats-proposal (action arrives first → proposal dropped)
- Race: proposal-beats-action (proposal refused → the late action applies)
- Idempotency: two proposals for the same token change nothing
- Convergence: one proposal on staggered clocks cannot split replicas
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
from holdem.p2p.timeout import DeadlineToken, FakeClock, RealClock

from crypto_gate import initialize_for_tests, NativeCryptoUnavailable

try:
    initialize_for_tests()
    importlib.import_module("holdem.p2p.elgamal")
except NativeCryptoUnavailable as exc:
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
        clk = clocks[i] if clocks and clocks[i] is not None else FakeClock()
        s = Session(
            is_host   = (i == 0),
            nickname  = f"P{i}",
            avatar_b64= "",
            transport = InMemoryTransport(bus, cid),
            clock     = clk,
            master_secret=b"test-session-secret",
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


def make_betting_table(n, clocks):
    """N sessions wired to a shared bus, each holding the same replica and
    no deal driver, so every deadline is a betting one. Every deadline
    starts at time 0 on its game's own clock."""
    from holdem.p2p.replica_table import ReplicaTable
    bus   = InMemoryBus()
    order = [f"peer{i}" for i in range(n)]
    sessions: dict[str, Session] = {}
    for i, cid in enumerate(order):
        s = Session(
            is_host   = (i == 0),
            nickname  = f"P{i}",
            avatar_b64= "",
            transport = InMemoryTransport(bus, cid),
            clock     = clocks[i],
            master_secret=b"test-session-secret",
        )
        s.local_conn_id = cid
        s.configure_seats(list(order))
        s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
        r = ReplicaTable(
            session_id="poker|" + "|".join(order), hand_no=1,
            names=[f"P{j}" for j in range(n)], stacks=[1000] * n,
            sb=5, bb=10, structure="No-Limit",
        )
        r.start_hand(0)
        s._replica = r
        s._hand_no = 1
        s._maybe_start_deadline()
        bus.register(cid, s)
        sessions[cid] = s
    return bus, sessions, order


def proposal_msg(token: DeadlineToken, hand: int = 1) -> dict:
    """The timeout_proposal a game sends for ``token``."""
    return {
        "type": "timeout_proposal",
        "hand": hand,
        "token": {
            "hand_id":    token.hand_id,
            "phase":      token.phase,
            "actor":      token.actor,
            "action_seq": token.action_seq,
        },
        "missing_seat": None,
    }


def session_with_deadline():
    """One session whose replica waits on B, with a betting deadline for B
    that started at 0 and has expired on its clock (now 31 s)."""
    bus = InMemoryBus()
    s = Session(is_host=True, nickname="H", avatar_b64="",
                transport=InMemoryTransport(bus, "A"),
                clock=FakeClock(31.0), master_secret=b"test-session-secret")
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
    token = DeadlineToken(
        hand_id    = "poker|A|B",
        phase      = "betting",
        actor      = "B",
        action_seq = r.next_seq,
    )
    s._current_deadline_token = token
    s._deadline_started_at   = 0.0
    return s, token


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
                    transport=InMemoryTransport(bus, "A"), clock=clk,
                    master_secret=b"test-session-secret")
        assert s._clock is clk

    def test_session_accepts_explicit_real_clock(self):
        bus = InMemoryBus()
        clock = RealClock()
        s = Session(is_host=True, nickname="T", avatar_b64="",
                    transport=InMemoryTransport(bus, "A"), clock=clock,
                    master_secret=b"test-session-secret")
        import time
        assert s._clock is clock
        assert abs(s._clock.monotonic() - time.monotonic()) < 1.0
        assert abs(s._clock.time() - time.time()) < 1.0

    def test_session_requires_a_clock(self):
        with pytest.raises(TypeError, match="clock"):
            Session(is_host=True, nickname="T", avatar_b64="",
                    transport=InMemoryTransport(InMemoryBus(), "A"),
                    master_secret=b"test-session-secret")


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
                    transport=InMemoryTransport(bus, "peer0"), clock=clk,
                    master_secret=b"test-session-secret")
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
# Betting timeout — check_deadlines reports; the consequence awaits a timekeeper
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
                    transport=InMemoryTransport(bus, "peer0"), clock=clk,
                    master_secret=b"test-session-secret")
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

    def test_check_deadlines_broadcasts_proposal_after_timeout(self):
        """The expired deadline is reported once, and the game reporting it
        applies nothing: no game applies a timeout proposal, the one that
        sends it included."""
        clk, bus, s, r = self._make_betting_session()
        assert s._current_deadline_token is not None
        seq, actor = r.next_seq, r.actor

        proposals = []
        original_broadcast = s._transport.broadcast
        s._transport.broadcast = lambda m: (proposals.append(m),
                                             original_broadcast(m))[-1]

        clk.advance(31.0)
        s.check_deadlines()
        assert [m.get("type") for m in proposals] == ["timeout_proposal"]
        assert r.next_seq == seq
        assert not r.engine.players[actor].folded

        s.check_deadlines()
        assert len(proposals) == 1, "the same wait was reported twice"

    def test_betting_timeout_folds_actor_facing_bet(self):
        """The consequence kept for the timekeeper (see
        Session._apply_timeout): an actor facing a bet is folded."""
        clk, bus, s, r = self._make_betting_session()
        actor_seat = r.actor
        to_call = r.engine.legal(actor_seat).get("to_call", 0)
        if to_call == 0:
            pytest.skip("initial actor has no bet to face")

        s._apply_timeout(s._current_deadline_token)
        assert r.engine.players[actor_seat].folded

    def test_betting_timeout_checks_actor_no_bet(self):
        """When there is no bet to face and the actor times out, they are checked.

        Tracked in https://github.com/nephrium83/texas-holdem/issues/11
        Requires a real mental-poker deal to reach a genuine postflop state;
        remove the skip below once the integration harness supports that.
        """
        # Find a game state where the first actor has no bet to face.
        # Preflop: UTG always faces a BB, so we need to get to postflop.
        bus, sessions, order = make_table(2)
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

        # The consequence kept for the timekeeper (see Session._apply_timeout).
        s0._apply_timeout(DeadlineToken(
            hand_id    = s0._deal_session_id(),
            phase      = "betting",
            actor      = actor_conn,
            action_seq = s0.replica.next_seq,
        ))
        bus.drain()

        # Actor was checked — action_on should have advanced past them
        new_actor = s0.replica.actor
        assert new_actor != actor_seat or s0.replica.phase != PHASE_BETTING

    def test_timeout_convergence_two_replicas(self):
        """Both deadlines have expired and one game reports its own. Neither
        game applies the proposal, the one that sent it included, so the
        hand is not voided and the replicas stay on one state."""
        clk0 = FakeClock()
        clk1 = FakeClock()
        bus, sessions, order = make_table(2, clocks=[clk0, clk1])
        s0, s1 = sessions[order[0]], sessions[order[1]]
        seq = s0.replica.next_seq

        sent = []
        original_broadcast = s0._transport.broadcast
        s0._transport.broadcast = lambda m: (sent.append(m),
                                              original_broadcast(m))[-1]

        clk0.advance(31.0)
        clk1.advance(31.0)
        s0.check_deadlines()
        bus.drain()

        assert any(m.get("type") == "timeout_proposal" for m in sent)
        for s in (s0, s1):
            assert not s.hand_voided, s.void_reason
            assert s.replica.next_seq == seq
        assert s0.replica.state_digest() == s1.replica.state_digest()


# ---------------------------------------------------------------------------
# Deal timeout
# ---------------------------------------------------------------------------

class TestDealTimeout:
    def test_deal_timeout_voids_hand_and_preserves_stacks(self):
        """What a future authenticated timekeeper notice would apply (no
        proposal does now): the hand is voided and stacks are unchanged."""
        clk = FakeClock()
        bus = InMemoryBus()
        order = ["A", "B"]
        sessions: dict[str, Session] = {}
        for i, cid in enumerate(order):
            s = Session(is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
                        transport=InMemoryTransport(bus, cid), clock=clk,
                        master_secret=b"test-session-secret")
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
                    transport=InMemoryTransport(bus, "host"),
                    clock=FakeClock(),
                    master_secret=b"test-session-secret")
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
    def test_a_current_proposal_is_refused_after_the_deadline(self):
        """The proposal names this game's current deadline, which has run
        out here too, and is still refused. A game's clock says when it may
        report a wait, never what a report does: deadlines start at
        different moments on different games."""
        s, token = session_with_deadline()
        seq, actor = s.replica.next_seq, s.replica.actor
        s._on_timeout_proposal("B", proposal_msg(token))
        assert s.replica.next_seq == seq
        assert s.replica.actor == actor
        assert not s.replica.engine.players[actor].folded

    def test_stale_token_dropped(self):
        s, token = session_with_deadline()
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
        s, token = session_with_deadline()
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
        s, token = session_with_deadline()
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
        s, token = session_with_deadline()
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
                    transport=InMemoryTransport(bus, "A"),
                    clock=FakeClock(),
                    master_secret=b"test-session-secret")
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
        s, token = session_with_deadline()
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
# Time-zero proposal (docs/CASUAL_P2P_RULES.md, "Time-zero timeout proposal")
# ---------------------------------------------------------------------------

class TestTimeZeroProposal:
    def test_a_proposal_at_time_zero_is_refused(self):
        """Seat 2 sends a timeout proposal the moment the deal's deadline
        starts. It names the current token, which any seat can copy, so it
        used to void the hand at once and hand the blinds back: an undo for
        whoever disliked its cards. Every other game refuses it."""
        clocks = [FakeClock() for _ in range(3)]
        bus, sessions, order = make_table(3, clocks=clocks)
        proposer = sessions[order[2]]
        token = proposer._current_deadline_token
        assert token is not None and token.phase == "deal_shuffle"

        proposal = proposal_msg(token, proposer._hand_no)
        proposer._send_hostless(proposal)          # stamps its author_seq
        bus.drain()

        for cid in order[:2]:
            s = sessions[cid]
            # Past ingress, so the handler is what refused it.
            seen = s._author_seq_seen.get((1, 2), {})
            assert proposal["author_seq"] in seen, cid
            # Current, not stale, so the refusal is not the token check's.
            assert s._current_deadline_token == token, cid
            assert token.action_seq == s.replica.next_seq, cid
            assert not s.hand_voided, s.void_reason
            assert s.hand_record is None, cid


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_second_proposal_is_harmless(self):
        """Two proposals with the same token: both are refused, so neither
        moves the hand."""
        s, token = session_with_deadline()
        before = s.replica.state_digest()
        s._on_timeout_proposal("B", proposal_msg(token))   # refused
        s._on_timeout_proposal("B", proposal_msg(token))   # refused again
        assert s.replica.state_digest() == before


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
        stale = {
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
        s0._on_timeout_proposal(non_actor, stale)

        # Proposal was silently dropped; actor has already moved on
        assert s0.replica.actor == actor_after

    def test_proposal_beats_action(self):
        """A proposal that reaches a game before the actor's action cannot
        pre-empt it: the proposal is refused, and the late action then
        applies on every game alike."""
        clk = FakeClock()
        bus, sessions, order = make_table(2, clocks=[clk, FakeClock()])
        s0 = sessions[order[0]]
        clk.advance(31.0)                  # s0's own deadline has expired
        actor_seat = s0.replica.actor
        actor_conn = order[actor_seat]
        non_actor  = next(c for c in order if c != actor_conn)

        if non_actor != order[0]:
            pytest.skip("local peer is the actor; swap needed")

        token = s0._current_deadline_token
        if token is None:
            pytest.skip("no deadline set")
        seq = s0.replica.next_seq

        # The proposal arrives at s0 first.
        s0._on_timeout_proposal(non_actor, proposal_msg(token, s0._hand_no))
        assert not s0.hand_voided, s0.void_reason
        assert s0.replica.next_seq == seq

        # Then the actor acts, late.
        assert sessions[actor_conn].send_bet_action("call") == "applied"
        bus.drain()
        assert s0.replica.next_seq == seq + 1
        assert (s0.replica.state_digest()
                == sessions[actor_conn].replica.state_digest())


# ---------------------------------------------------------------------------
# Convergence across multiple replicas
# ---------------------------------------------------------------------------

class TestConvergence:
    def test_one_proposal_cannot_split_replicas_on_staggered_clocks(self):
        """Each game starts a deadline when it reaches it, so deadlines are
        staggered: here the late watcher's started 21 s after the early
        one's, and the early one reports its expired deadline. Judged by
        each game's own clock, the proposal folded the actor on the early
        game alone. Judged by the token alone, it folded the actor
        everywhere but on the actor's own game, which sets no deadline on
        itself. Every game refuses it, so all three stay on one state, and
        the actor's real action then moves them together."""
        clocks = [FakeClock() for _ in range(3)]
        bus, sessions, order = make_betting_table(3, clocks)
        actor = sessions[order[0]].replica.actor
        early, late = [i for i in range(3) if i != actor]
        token = sessions[order[early]]._current_deadline_token
        assert token is not None and token.phase == "betting"
        assert token.actor == order[actor]
        assert sessions[order[late]]._current_deadline_token == token
        assert sessions[order[actor]]._current_deadline_token is None
        seq = token.action_seq

        clocks[early].advance(31.0)        # expired
        clocks[late].advance(10.0)         # 20 s to go
        reporter = sessions[order[early]]
        sent = []
        original_broadcast = reporter._transport.broadcast
        reporter._transport.broadcast = lambda m: (sent.append(m),
                                                    original_broadcast(m))[-1]
        reporter.check_deadlines()
        bus.drain()

        proposal = next(m for m in sent if m.get("type") == "timeout_proposal")
        for i in (late, actor):
            # Past ingress, so the handler is what refused it.
            seen = sessions[order[i]]._author_seq_seen.get((1, early), {})
            assert proposal["author_seq"] in seen, i
        digests = {s.replica.state_digest() for s in sessions.values()}
        assert len(digests) == 1, f"one proposal split the replicas: {digests}"
        for cid, s in sessions.items():
            assert s.replica.next_seq == seq, cid
            assert not s.replica.engine.players[actor].folded, cid
            assert not s.hand_voided, cid

        # Control: the replicas still move together on a real action.
        assert sessions[order[actor]].send_bet_action("call") == "applied"
        bus.drain()
        digests = {s.replica.state_digest() for s in sessions.values()}
        assert len(digests) == 1, f"replicas diverged on an action: {digests}"
        for cid, s in sessions.items():
            assert s.replica.next_seq == seq + 1, cid

    def test_no_sleep_anywhere(self):
        """Confirm the timeout module has no sleep() calls — the word may
        appear in comments or docstrings, so check for the call pattern."""
        import re
        import holdem.p2p.timeout as tm
        import inspect
        src = inspect.getsource(tm)
        assert not re.search(r'\bsleep\s*\(', src), \
            "Found a sleep() call in holdem/p2p/timeout.py"
