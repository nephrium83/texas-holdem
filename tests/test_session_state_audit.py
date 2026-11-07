"""Session-state repairs: cross-owner handoffs C94 (typed expected refusal)
and C116 (monotonic startup evidence), and audit findings #53, #64, #65,
#67 and #75."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from crypto_gate import initialize_for_tests, NativeCryptoUnavailable

try:
    initialize_for_tests()
    import test_session_hand as H
    import test_replica_table as RT
    from holdem.p2p import deck_audit as da
    from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
    from holdem.p2p.replica_table import (
        MAX_PENDING, PHASE_SHOWDOWN, UnverifiedHolesError)
    from holdem.p2p.session import CommandRefused, Session
except NativeCryptoUnavailable as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}", allow_module_level=True)


# ----------------------------------------------- C94 typed expected refusal

def test_refusals_are_typed_and_still_runtime_errors():
    assert issubclass(CommandRefused, RuntimeError)
    bus, sessions, order = H.make_table(2)
    guest = sessions[order[1]]
    with pytest.raises(CommandRefused, match="Only the host"):
        guest.start_game({})


def test_changing_seats_mid_hand_is_a_refusal():
    bus, sessions, order = H.make_table(2)
    with pytest.raises(CommandRefused, match="active hand"):
        sessions[order[0]].configure_seats(list(order))


def test_anything_on_a_terminated_session_is_a_refusal():
    bus, sessions, order = H.make_table(2)
    host = sessions[order[0]]
    host.terminate("aborted", "test")
    with pytest.raises(CommandRefused, match="terminated"):
        host.start_p2p_hand(hand_no=2, names=["P0", "P1"], stacks=[500, 500],
                            sb=5, bb=10, button=0)


def _raises_by_message():
    """(exception class, message text) for every raise in session.py."""
    src = Path(__file__).resolve().parents[1].joinpath(
        "holdem", "p2p", "session.py").read_text(encoding="utf-8").splitlines()
    out = []
    for i, line in enumerate(src):
        for kind in ("CommandRefused", "RuntimeError"):
            if f"raise {kind}(" in line:
                out.append((kind, " ".join(l.strip() for l in src[i:i + 3])))
    return out


REFUSALS = ["Only the host can start the game",
            "cannot change seat order during an active hand",
            "cannot start a game: session terminated",
            "cannot start a hand: session terminated",
            "cannot begin a hand: session terminated",
            "cannot seat a table: session terminated",
            "local seat is not dealt in",
            "policy cannot change"]
INVARIANTS = ["mental-deal flush did not terminate",
              "session protocol state mutated outside the owner",
              "cannot bind seat keys",
              "cannot build a deal context",
              "local seat not in seat order",
              "seat_local_table is for local tables only"]


def test_refusals_and_invariants_are_classified():
    """Every expected refusal is a CommandRefused; every invariant failure
    stays a plain RuntimeError, so a caller that narrows its catch to
    CommandRefused lets real defects surface."""
    raises = _raises_by_message()
    for phrase in REFUSALS:
        kinds = {k for k, m in raises if phrase in m}
        assert kinds == {"CommandRefused"}, (phrase, kinds)
    for phrase in INVARIANTS:
        kinds = {k for k, m in raises if phrase in m}
        assert kinds == {"RuntimeError"}, (phrase, kinds)


# --------------------------------------- C116 monotonic startup evidence

def test_hands_dealt_in_counts_each_dealt_hand_and_never_resets():
    bus, sessions, order = H.make_table(2)
    assert all(sessions[c].hands_dealt_in == 1 for c in order)
    H.act(bus, sessions, order, "fold")
    for c in order:
        s = sessions[c]
        assert s.hand_result is not None
    for c in order:
        sessions[c].next_p2p_hand()
    bus.drain()
    assert all(sessions[c].hands_dealt_in == 2 for c in order)


def test_a_seat_that_never_got_its_deal_reports_zero():
    """A seat whose deal traffic never arrives has a replica but no holes:
    exactly the 'reported as started, frozen at Dealing' case."""
    bus = InMemoryBus()
    order = ["p0", "p1"]
    sessions = {}
    for i, cid in enumerate(order):
        s = Session(is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
                    transport=InMemoryTransport(bus, cid),
                    clock=H._DependencyClock(), master_secret=bytes(range(32)))
        s.local_conn_id = cid
        s.configure_seats(list(order))
        s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
        s._adopt_continuation_policy(Session.CONTINUATION_END_TABLE)
        bus.register(cid, s)
        sessions[cid] = s
    for cid in order:
        sessions[cid].start_p2p_hand(hand_no=1, names=["P0", "P1"],
                                     stacks=[500, 500], sb=5, bb=10, button=0)
    # nothing delivered: replicas exist, deals cannot complete
    assert all(sessions[c].replica is not None for c in order)
    assert all(sessions[c].hands_dealt_in == 0 for c in order)
    bus.drain()
    assert all(sessions[c].hands_dealt_in == 1 for c in order)


# -------------------------------------------- #75 non-participant voids

def test_a_seat_not_dealt_in_cannot_void_the_hand(monkeypatch):
    bus, sessions, order = H.make_table(2)
    s = sessions[order[0]]
    monkeypatch.setattr(s, "_hand_msg_ok", lambda conn_id, msg: True)
    assert 2 not in s.replica.seats_dealt
    s._on_hand_void("peer9", {"type": "hand_void", "hand": s._hand_no,
                              "seat": 2, "reason": "griefing"})
    assert not s.hand_voided
    s._on_hand_void(order[1], {"type": "hand_void", "hand": s._hand_no,
                               "seat": 1, "reason": "real problem"})
    assert s.hand_voided


# ------------------------------------------- #65 unaudited showdown holes

def _to_showdown(reps):
    seq = 0
    while reps[0].phase != PHASE_SHOWDOWN:
        if reps[0].phase == "street_over":
            n = {"preflop": 3, "flop": 1, "turn": 1}[reps[0].engine.street]
            from holdem.engine import FULL_DECK
            used = {(c.v, c.s) for r in reps for c in r.engine.board}
            cards = [c for c in reversed(FULL_DECK) if (c.v, c.s) not in used][:n]
            for r in reps:
                r.advance_street(cards)
            continue
        RT.apply_all(reps, seq, reps[0].actor, "call")
        seq += 1


def _real_holes():
    from holdem.engine import Card
    return {0: [Card(2, 0), Card(3, 1)], 1: [Card(4, 2), Card(5, 3)]}


def test_contested_showdown_refuses_dummy_holes():
    reps = RT.make_replicas(1, ["A", "B"], [100, 100])
    _to_showdown(reps)
    r = reps[0]
    r.set_own_hole(0, _real_holes()[0])            # only the local seat
    with pytest.raises(UnverifiedHolesError, match=r"\[1\]"):
        r.finish(force_tabled=True)
    assert r.result is None


def test_partial_audit_map_is_refused_and_full_map_settles():
    reps = RT.make_replicas(1, ["A", "B"], [100, 100])
    _to_showdown(reps)
    r = reps[0]
    r.set_all_holes({0: _real_holes()[0]})
    with pytest.raises(UnverifiedHolesError):
        r.finish(force_tabled=True)
    r.set_all_holes(_real_holes())
    assert r.finish(force_tabled=True) is not None


def test_duplicate_or_impossible_hole_cards_are_refused():
    from holdem.engine import Card
    reps = RT.make_replicas(1, ["A", "B"], [100, 100])
    _to_showdown(reps)
    r = reps[0]
    dup = {0: [Card(2, 0), Card(3, 1)], 1: [Card(2, 0), Card(5, 3)]}
    with pytest.raises(ValueError, match="twice"):
        r.set_all_holes(dup)
    board_card = r.engine.board[0]
    with pytest.raises(ValueError, match="twice"):
        r.set_all_holes({0: [board_card, Card(9, 1)], 1: _real_holes()[1]})
    with pytest.raises(ValueError, match="no such seat"):
        r.set_all_holes({7: _real_holes()[0]})


def test_fold_out_needs_no_hole_audit():
    reps = RT.make_replicas(1, ["A", "B"], [100, 100])
    r = reps[0]
    RT.apply_all(reps, 0, r.actor, "fold")
    assert r.finish() is not None


# ------------------------------------------------ #67 honest verdicts

def test_out_of_order_verdicts_say_what_happened():
    reps = RT.make_replicas(1, ["A", "B", "C"], [500, 500, 500])
    r = reps[0]
    assert r.apply_action(2, 0, "call") == "buffered"
    assert r.apply_action(2, 0, "call") == "buffered"           # same again
    assert r.apply_action(2, 0, "fold") == "rejected"           # conflicting
    assert r.apply_action(MAX_PENDING + 1, 0, "call") == "overflow"   # too far
    for seq in range(3, MAX_PENDING + 1):
        assert r.apply_action(seq, 0, "call") == "buffered"
    assert len(r._pending) == MAX_PENDING - 1
    assert r.apply_action(1, 0, "call") == "buffered"           # last free slot
    assert len(r._pending) == MAX_PENDING
    assert r.next_seq == 0


def test_a_full_buffer_reports_overflow_not_buffered():
    reps = RT.make_replicas(1, ["A", "B"], [500, 500])
    r = reps[0]
    for seq in range(1, MAX_PENDING + 1):
        r._pending[seq] = (0, "call", 0)
    r._pending.pop(5)
    r._pending[MAX_PENDING + 1] = (0, "call", 0)                 # still full
    assert r.apply_action(5, 0, "call") == "overflow"


# ---------------------------------------- #53 indeterminate attribution

def test_invalid_share_evidence_is_never_pinned_on_a_shuffler():
    good = da.AuditReport(ok=True, cards=[])
    bad_shares = da.AuditReport(ok=False, cards=[], bad_seats=[2],
                                shares_valid=False)
    bad_deck = da.AuditReport(ok=False, cards=[])
    assert da.first_corrupt_round([good, bad_shares, bad_deck]) is None
    assert da.first_corrupt_round([good, bad_deck]) == 1
    assert da.first_corrupt_round([good, good]) is None


# --------------------------------------------- #64 in-memory isolation

def test_inmemory_recipients_never_share_a_message_object():
    bus = InMemoryBus()
    seen = {}

    class Sink:
        def __init__(self, name):
            self.name = name

        def handle_message(self, conn_id, msg):
            seen.setdefault(self.name, []).append(msg)
            msg["payload"]["nested"].append(self.name)       # hostile mutation

    for name in ("a", "b", "c"):
        bus.register(name, Sink(name))
    sender = InMemoryTransport(bus, "a")
    original = {"type": "x", "payload": {"nested": []}}
    sender.broadcast(original)
    original["payload"]["nested"].append("sender-after-send")
    bus.drain()
    assert seen["b"][0]["payload"]["nested"] == ["b"]
    assert seen["c"][0]["payload"]["nested"] == ["c"]
