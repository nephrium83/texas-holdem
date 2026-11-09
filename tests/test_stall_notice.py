"""Audit #194: a stalled deal ends by the timekeeper's signed notice.

The defect: on a non-casual table a withheld deal contribution hung the
hand for good. ``check_deadlines`` had no production caller, and when
driven it reported the wait once, sent a ``timeout_proposal`` no game
applies, and cleared the deadline.

The first fix (PR #138) terminated locally when the local deadline expired,
and was withdrawn. Three measured asymmetries killed it, and they are one
cause -- every predicate available to a single peer is LOCAL:

  1. the acting peer is exempt from its own betting deadline, so ending on
     any expired deadline ended peer0/peer2 while peer1, the actor, lived;
  2. ``deal_done()`` is only true after the post-hand audit, so an ordinary
     betting turn carries a "deal_shuffle" label, and ending on the label
     killed a healthy table;
  3. ``public_waits()`` is irreducibly local -- omitting one ``deal_share``
     ended that peer alone while the others played on.

Narrowing cannot rescue it either, because two cases need OPPOSITE rules.
So the decision has to travel: one seat decides, the decision is signed,
and receivers follow it WITHOUT consulting their own state.

Every test below asserts its own preconditions before concluding anything.
Three tests written during #194 passed because the fixture quietly avoided
the condition under test, so a precondition that stops holding has to fail
here rather than pass quietly.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.session import (AUTHOR_MODE_WIRE, Session,  # noqa: E402
                                _STALL_NOTICE_PHASES)

from tests.test_session_prevention import SignedBus, signed_table  # noqa: E402

PHASE_TIMEOUT = 31.0            # past the 30 s deal_shuffle default


class OmittingSignedBus(SignedBus):
    """A signed bus that can drop one message for ONE recipient.

    Per-recipient omission is what selective withholding looks like on the
    wire, and the inherited drain cannot express it: it fans a broadcast
    out to every registered session. So the fan-out is expanded here, the
    way ChaosBus does it, and only then filtered -- the envelope handling,
    signing and verification stay SignedBus's.
    """

    def __init__(self, clock, omit=None):
        super().__init__(clock)
        #: ``omit(from_conn, to_conn, envelope) -> bool``; True drops it for
        #: that recipient alone. Every other peer still receives it.
        self.omit = omit
        self.dropped = []

    def drain(self, max_steps: int = 100000) -> int:
        steps = 0
        while self._queue:
            if steps >= max_steps:
                raise RuntimeError("OmittingSignedBus.drain exceeded max_steps")
            from_conn, to_conn, msg = self._queue.pop(0)
            if to_conn is not None:
                targets = [to_conn] if to_conn in self._sessions else []
            else:
                targets = [c for c in self._sessions if c != from_conn]
            for cid in targets:
                steps += 1
                if self.omit is not None and self.omit(from_conn, cid, msg):
                    self.dropped.append((from_conn, cid, msg.get("type")))
                    continue
                session = self._sessions.get(cid)
                if session is not None:
                    session.handle_message(from_conn, dict(msg))
        return steps


def stalled_table(tmp_path, n=3, omit=None, hand=1):
    """A signed, wire-mode, end-table-v1 table part-way through a deal.

    ``signed_table`` builds the production arrangement -- real identities,
    the real admission handshake, seats frozen onto the keys each peer
    signs with -- and declares CONTINUATION_END_TABLE, which is exactly the
    non-casual policy this exchange is scoped to.
    """
    bus, sessions, order, keys = signed_table(tmp_path, n)
    bus.omit = omit
    bus.__class__ = OmittingSignedBus
    bus.dropped = []
    names = [f"P{i}" for i in range(n)]
    for cid in order:
        sessions[cid].start_p2p_hand(hand_no=hand, names=names,
                                     stacks=[500] * n, sb=5, bb=10, button=0)
    bus.drain()
    return bus, sessions, order


def tick_all(bus, sessions, order, seconds=PHASE_TIMEOUT, rounds=3):
    """Advance the shared clock and run each peer's reliability tick."""
    for _ in range(rounds):
        sessions[order[0]]._clock.advance(seconds)
        for cid in order:
            sessions[cid].reliability_tick()
        bus.drain()


def terminal(sessions, order):
    return {cid: sessions[cid].terminal_state for cid in order}


# ---------------------------------------------------------------------------
# The arrangement itself. Guards everything below it: if this table ran in
# compat mode or adopted the casual policy, every test here would pass by
# refusing the notice for the wrong reason.
# ---------------------------------------------------------------------------

def test_the_table_under_test_is_the_non_casual_wire_arrangement(tmp_path):
    bus, sessions, order = stalled_table(tmp_path, 3)
    for cid in order:
        s = sessions[cid]
        assert s.author_mode == AUTHOR_MODE_WIRE, f"{cid} is not in wire mode"
        assert not s._casual_reliability, f"{cid} adopted casual reliability"
        assert s.continuation_policy == Session.CONTINUATION_END_TABLE
        assert s.terminal_state is None, f"{cid} was already terminal"
    # The host is dealt into the hand, so it is the timekeeper on every peer
    # -- the agreement that makes one signed notice followable.
    keepers = {cid: sessions[cid]._stall_timekeeper_seat() for cid in order}
    assert set(keepers.values()) == {0}, f"peers disagree on timekeeper: {keepers}"


# ---------------------------------------------------------------------------
# The case #194 is actually about.
# ---------------------------------------------------------------------------

def test_a_withheld_shuffle_round_ends_every_peer_alike(tmp_path):
    """A round withheld from one peer blocks the chain, so the timekeeper
    sees the stall and ends the table for everyone, with one reason."""
    victim = "peer1"

    def omit(from_conn, to_conn, env):
        return env.get("type") == "deck_round" and to_conn == victim

    bus, sessions, order = stalled_table(tmp_path, 3, omit=omit)

    # Preconditions. Without these the test could pass having stalled for
    # some other reason, or not stalled at all.
    assert bus.dropped, "nothing was actually withheld"
    keeper = sessions[order[0]]
    deal = keeper._deal_driver.deal
    assert not deal.is_shuffle_complete(), "the shuffle finished anyway"
    assert deal.public_waits(), (
        "the timekeeper sees nothing missing, so this case cannot be the one "
        "the notice covers")
    token = keeper._current_deadline_token
    assert token is not None and token.phase in _STALL_NOTICE_PHASES, \
        f"no deal deadline to expire: {token}"
    assert all(sessions[c].terminal_state is None for c in order)

    tick_all(bus, sessions, order)

    states = terminal(sessions, order)
    assert all(v == Session.ABORTED_PROTOCOL for v in states.values()), \
        f"peers did not all end on the protocol abort: {states}"
    reasons = {sessions[c].terminal_reason for c in order}
    assert len(reasons) == 1, f"peers ended for different reasons: {reasons}"
    assert "stalled deal" in reasons.pop()
    # The notice names its sender, and every record agrees who that was.
    keepers = {sessions[c].terminal_record.initiating_seat for c in order}
    assert keepers == {0}, f"records disagree on the timekeeper: {keepers}"


def test_nothing_is_voided_so_nothing_is_refunded(tmp_path):
    """Terminate, never void. A void refunds, and the refund is what made
    the time-zero timeout proposal a profitable undo button."""
    def omit(from_conn, to_conn, env):
        return env.get("type") == "deck_round" and to_conn == "peer1"

    bus, sessions, order = stalled_table(tmp_path, 3, omit=omit)
    tick_all(bus, sessions, order)

    for cid in order:
        s = sessions[cid]
        assert s.terminal_state == Session.ABORTED_PROTOCOL, cid
        assert not s.hand_voided, f"{cid} voided the hand: {s.void_reason}"
        assert s.hand_result is None, f"{cid} settled a stalled hand"


# ---------------------------------------------------------------------------
# The guards. One test per refusal, each the named mutant's target.
# ---------------------------------------------------------------------------

def test_a_healthy_table_is_not_ended_by_a_slow_turn(tmp_path):
    """Codex's second P1, and the dangerous one. _maybe_start_deadline
    labels a deadline "deal_shuffle" until deal_done(), which is only true
    after the POST-HAND AUDIT -- so an ordinary betting turn on a healthy
    table carries that label. Ending on the label alone terminated all
    three peers while nobody had withheld anything."""
    bus, sessions, order = stalled_table(tmp_path, 3)

    keeper = sessions[order[0]]
    deal = keeper._deal_driver.deal
    token = keeper._current_deadline_token
    # The conditions this test exists for, asserted rather than assumed.
    assert deal.is_shuffle_complete() and deal.hole_complete(), \
        "the deal really is incomplete, so this is a genuine stall"
    assert deal.public_waits() == [], \
        f"something really is missing: {deal.public_waits()}"
    assert token is not None and token.phase in _STALL_NOTICE_PHASES, (
        f"without the misleading label this test proves nothing: {token}")

    tick_all(bus, sessions, order)

    states = terminal(sessions, order)
    assert all(v is None for v in states.values()), \
        f"a healthy table was ended by a slow turn: {states}"


def test_a_seat_that_is_not_the_timekeeper_cannot_end_the_table(tmp_path):
    """The authority check. Without it any seat could copy the current wait
    and end the table -- the timeout_proposal undo button, reinvented."""
    bus, sessions, order = stalled_table(tmp_path, 3)
    victim = sessions["peer0"]
    assert victim._stall_timekeeper_seat() == 0, "peer0 is the timekeeper"

    notice = {"type": "stall_notice", "hand": victim._hand_no, "seat": 2,
              "token": {"hand_id": victim._deal_session_id(),
                        "phase": "deal_shuffle", "actor": None,
                        "action_seq": victim.replica.next_seq},
              "waits": [{"phase": "shuffle", "actor": 1, "detail": "2"}],
              "author_seq_holes": []}
    victim._on_stall_notice("peer2", notice)

    assert victim.terminal_state is None, \
        f"seat 2 ended the table it does not keep time for: {victim.terminal_reason}"


def test_a_betting_phase_notice_is_refused(tmp_path):
    """OWNER_DECISION_BETTING_STALL_20261108: a slow betting actor on a
    non-casual table stays the host application's business. The phase scope
    enforces that in the protocol, not only in the sender."""
    bus, sessions, order = stalled_table(tmp_path, 3)
    s = sessions["peer0"]
    assert "betting" not in _STALL_NOTICE_PHASES

    s._on_stall_notice("peer0", {
        "type": "stall_notice", "hand": s._hand_no, "seat": 0,
        "token": {"hand_id": s._deal_session_id(), "phase": "betting",
                  "actor": "peer1", "action_seq": s.replica.next_seq},
        "waits": [{"phase": "action", "actor": 1, "detail": ""}],
        "author_seq_holes": []})

    assert s.terminal_state is None, \
        f"a betting notice ended the table: {s.terminal_reason}"


def test_a_notice_for_another_hand_is_refused(tmp_path):
    bus, sessions, order = stalled_table(tmp_path, 3)
    s = sessions["peer0"]
    s._on_stall_notice("peer0", {
        "type": "stall_notice", "hand": s._hand_no + 1, "seat": 0,
        "token": {"hand_id": s._deal_session_id(), "phase": "deal_shuffle",
                  "actor": None, "action_seq": s.replica.next_seq},
        "waits": [], "author_seq_holes": []})
    assert s.terminal_state is None, "a later hand's notice ended this one"


def test_a_compat_table_refuses_the_notice():
    """In compat mode seat authority falls back to the delivering conn_id,
    so nothing proves the sender is the timekeeper -- it would be
    trust-whoever-handed-us-the-bytes, the failure Control N on PR #31
    found. Compat tables keep their existing behaviour and still hang.

    Uses the compat fixture deliberately: asserting the refusal on a wire
    table would prove nothing about the mode check.
    """
    from tests.test_session_hand import make_table as compat_table

    bus, sessions, order = compat_table(3)
    s = sessions["peer0"]
    assert s.author_mode != AUTHOR_MODE_WIRE,         "this fixture is not in compat mode, so the test proves nothing"

    s._on_stall_notice("peer0", {
        "type": "stall_notice", "hand": s._hand_no, "seat": 0,
        "token": {"hand_id": s._deal_session_id(), "phase": "deal_shuffle",
                  "actor": None, "action_seq": s.replica.next_seq},
        "waits": [{"phase": "shuffle", "actor": 1, "detail": "2"}],
        "author_seq_holes": []})

    assert s.terminal_state is None,         f"a compat table was ended by a stall notice: {s.terminal_reason}"


def test_a_casual_table_refuses_the_notice(tmp_path):
    """Casual tables recover under rule 9's signed drop. A second ending
    path competing with it would be a regression, so it is refused."""
    bus, sessions, order = stalled_table(tmp_path, 3)
    s = sessions["peer0"]
    s._continuation_policy = Session.CONTINUATION_SURVIVORS
    assert s._casual_reliability, "the policy switch did not take"

    s._on_stall_notice("peer0", {
        "type": "stall_notice", "hand": s._hand_no, "seat": 0,
        "token": {"hand_id": s._deal_session_id(), "phase": "deal_shuffle",
                  "actor": None, "action_seq": s.replica.next_seq},
        "waits": [{"phase": "shuffle", "actor": 1, "detail": "2"}],
        "author_seq_holes": []})

    assert s.terminal_state is None, \
        f"a casual table was ended by a stall notice: {s.terminal_reason}"


# ---------------------------------------------------------------------------
# The test that proves the DESIGN rather than the code.
# ---------------------------------------------------------------------------

def test_the_receiver_follows_the_notice_without_consulting_its_own_waits(tmp_path):
    """The whole design in one assertion.

    public_waits() is irreducibly local, and no subset of wait phases is
    symmetric: a hole share withheld from one peer leaves it waiting on
    "holes" while the others wait on nothing, so ending on "holes" splits;
    a withheld shuffle round leaves it on "shuffle" while the others are on
    "holes", so NOT ending on "holes" splits.

    So a receiver with NOTHING missing locally must still follow the
    notice. If a future change makes the receiver re-check its own waits,
    this test fails -- which is the point, because that change would look
    like a safety improvement and would reinstate the split.
    """
    bus, sessions, order = stalled_table(tmp_path, 3)
    s = sessions["peer2"]
    deal = s._deal_driver.deal
    assert deal.public_waits() == [], (
        "this peer must be missing NOTHING for the test to mean anything: "
        f"{deal.public_waits()}")
    assert s._stall_timekeeper_seat() == 0

    s._on_stall_notice("peer0", {
        "type": "stall_notice", "hand": s._hand_no, "seat": 0,
        "token": {"hand_id": s._deal_session_id(), "phase": "deal_shuffle",
                  "actor": None, "action_seq": s.replica.next_seq},
        "waits": [{"phase": "shuffle", "actor": 1, "detail": "2"}],
        "author_seq_holes": []})

    assert s.terminal_state == Session.ABORTED_PROTOCOL, (
        "a peer that saw nothing missing did not follow the timekeeper's "
        "notice, so the outcome depends on local state again")


def test_the_notice_carries_the_senders_forensics_into_the_record(tmp_path):
    """The waits are forensics for the terminal record, not a condition.
    An empty or malformed list must still end the table rather than block
    it -- requiring substance at the receiver is requiring a local
    predicate."""
    bus, sessions, order = stalled_table(tmp_path, 3)
    s = sessions["peer2"]
    events = []

    class _Sink:
        """_safe_emit writes to the configured sink, not to a callback."""

        def emit(self, payload):
            events.append(payload)

    s._log_sink = _Sink()

    s._on_stall_notice("peer0", {
        "type": "stall_notice", "hand": s._hand_no, "seat": 0,
        "token": {"hand_id": s._deal_session_id(), "phase": "deal_decrypt",
                  "actor": None, "action_seq": s.replica.next_seq},
        "waits": "not a list", "author_seq_holes": [{"seat": 1}]})

    assert s.terminal_state == Session.ABORTED_PROTOCOL, \
        "a malformed waits list blocked the ending"
    stopping = [e for e in events if e.get("event") == "sidecar_stopping"]
    assert stopping, f"no sidecar_stopping payload was emitted: {events}"
    assert stopping[-1]["timekeeper_seat"] == 0
    assert stopping[-1]["token_phase"] == "deal_decrypt"
    assert stopping[-1]["public_waits"] == [],         "a malformed waits list must be reported as empty, not propagated"
    assert stopping[-1]["author_seq_holes"] == [{"seat": 1}]


if __name__ == "__main__":
    # Preserve fixtures, parametrization and a failing process exit status.
    raise SystemExit(pytest.main([__file__, *sys.argv[1:]]))
