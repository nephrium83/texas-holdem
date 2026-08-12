"""Signed per-author, per-hand sequencing of hostless messages.

Every hostless message carries ``author_seq``: a number the author stamps,
counted per (hand, seat), covering all eight hostless types from one place
on the send path and validated in one place on the receive path.

What it is for
--------------
The star topology makes the host a relay for every joiner-to-joiner
message (see ``Session._maybe_relay``). A relay that can drop or replay
traffic is a position of trust, and sequencing is what makes abuse of it
visible to the recipients rather than to nobody:

* a **replay** is dropped -- each (hand, seat, author_seq) is applied at
  most once, so a host cannot re-inject a seat's message at the table;
* a **renumber or strip** is impossible without breaking the envelope --
  ``author_seq`` is stamped before signing and therefore inside the
  Ed25519 pre-image;
* a **hole** is observable through ``author_seq_holes()``.

What it deliberately does NOT do
--------------------------------
It does not void a hand the moment a number arrives out of order. Delivery
may reorder -- ``tests/test_convergence_chaos.py`` reorders on purpose and
still requires convergence -- so a gap and honest reordering are the same
observation until the stream stops. An earlier revision voided on sight and
killed 22 healthy hands in that suite; the tests below pin the corrected
behaviour so it cannot come back.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.session import AUTHOR_SEQ_START, Session

try:
    from holdem.p2p import ristretto as R           # noqa: F401
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


HOSTLESS = ("key_announce", "deck_round", "deal_share", "audit_open",
            "bet_action", "hand_void", "session_end", "timeout_proposal")


class RecordingBus(InMemoryBus):
    """An InMemoryBus that keeps every message it was asked to carry."""

    def __init__(self):
        super().__init__()
        self.carried = []

    def enqueue(self, from_conn, to_conn, msg):
        self.carried.append((from_conn, dict(msg)))
        super().enqueue(from_conn, to_conn, msg)


def make_table(n, bus=None):
    """n Sessions on one bus; seat index i is peer i."""
    bus = bus if bus is not None else InMemoryBus()
    order = [f"peer{i}" for i in range(n)]
    sessions = {}
    for i, cid in enumerate(order):
        s = Session(is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
                    transport=InMemoryTransport(bus, cid))
        s.local_conn_id = cid
        s.configure_seats(list(order))
        bus.register(cid, s)
        sessions[cid] = s
    return bus, sessions, order


# ── stamping ──────────────────────────────────────────────────────────────

def test_a_hand_stamps_every_hostless_message_it_sends():
    """Whatever the deal emits, it is numbered -- no type opts out.

    Asserted over what actually crossed the bus rather than over a list of
    types this test remembers to name, because the failure being guarded is
    a send path that forgot to go through the one stamping point.
    """
    bus = RecordingBus()
    _, sessions, order = make_table(2, bus=bus)
    for cid in order:
        sessions[cid].begin_hand(hand_no=1, button=0)
    bus.drain()

    hostless = [(frm, m) for frm, m in bus.carried
                if m.get("type") in HOSTLESS]
    assert hostless, "the hand emitted no hostless messages at all"
    unstamped = [m for _, m in hostless if "author_seq" not in m]
    assert not unstamped, (
        "hostless messages left this peer without an author_seq: "
        f"{[m.get('type') for m in unstamped]}")


def test_each_authors_numbers_start_at_zero_and_do_not_repeat():
    """Per (hand, seat): contiguous from AUTHOR_SEQ_START, no duplicates.

    Contiguity is required of the SENDER even though the receiver tolerates
    reordering -- a sender that skipped numbers would manufacture holes that
    look exactly like suppression.
    """
    bus = RecordingBus()
    _, sessions, order = make_table(2, bus=bus)
    for cid in order:
        sessions[cid].begin_hand(hand_no=1, button=0)
    bus.drain()

    streams = {}
    for frm, m in bus.carried:
        if m.get("type") not in HOSTLESS or "author_seq" not in m:
            continue
        streams.setdefault((m.get("hand"), frm), []).append(m["author_seq"])

    assert streams, "no sequenced hostless traffic observed"
    for key, nums in streams.items():
        assert len(nums) == len(set(nums)), f"{key} reused a number: {nums}"
        assert sorted(nums) == list(
            range(AUTHOR_SEQ_START, AUTHOR_SEQ_START + len(nums))), (
            f"{key} is not contiguous from {AUTHOR_SEQ_START}: {sorted(nums)}")


@pytest.mark.parametrize("mtype", HOSTLESS)
def test_the_stamping_point_stamps_every_type(mtype):
    """Every one of the eight, driven through _send_hostless directly.

    The bus-observation test above cannot cover this: a single hand emits
    only the types that hand happens to need, so a type skipped inside the
    stamping point -- rather than by a call site that bypassed it -- goes
    unobserved. A control that made bet_action skip the stamp fired nothing
    until this existed. The two tests catch opposite failures: this one a
    hole INSIDE the chokepoint, that one a send path that never reached it.
    """
    bus = RecordingBus()
    _, sessions, order = make_table(2, bus=bus)
    sessions[order[0]]._send_hostless({"type": mtype, "hand": 1, "seat": 0})

    sent = [m for _, m in bus.carried if m.get("type") == mtype]
    assert sent, f"{mtype} never reached the transport"
    assert "author_seq" in sent[-1], (
        f"{mtype} left the stamping point without an author_seq")


# ── receiving ─────────────────────────────────────────────────────────────

def _first_hostless_from(bus, frm):
    for f, m in bus.carried:
        if f == frm and m.get("type") in HOSTLESS and "author_seq" in m:
            return m
    return None


def test_a_replayed_message_is_not_applied_twice():
    """The property that makes a relay unable to re-inject traffic.

    Asserted on whether the message reaches the state machine, not on the
    bookkeeping set. A duplicate leaves that set unchanged whether it was
    dropped or applied, so a test watching the set passes even when nothing
    is being suppressed -- which a control confirmed by breaking the drop
    and firing nothing.
    """
    bus = RecordingBus()
    _, sessions, order = make_table(2, bus=bus)
    for cid in order:
        sessions[cid].begin_hand(hand_no=1, button=0)
    bus.drain()

    victim = sessions[order[1]]
    original = _first_hostless_from(bus, order[0])
    assert original is not None, "peer0 sent no sequenced hostless message"
    assert original["type"] in ("key_announce", "deck_round", "deal_share",
                                "audit_open"), "expected a deal-driver type"

    applied = []
    victim._deal_driver.handle = lambda m: applied.append(m)

    # Control within the test: an UNSEEN number must reach the driver, so a
    # later "nothing arrived" assertion cannot pass for the wrong reason.
    fresh = dict(original)
    fresh["author_seq"] = 500
    victim.handle_message(order[0], fresh)
    assert len(applied) == 1, (
        "precondition failed: an unseen message did not reach the state "
        f"machine, so this test cannot detect suppression ({applied})")

    # The same number again, exactly as a replaying relay would deliver it.
    victim.handle_message(order[0], dict(fresh))
    assert len(applied) == 1, (
        "a replayed message was applied a second time")


def test_reordering_does_not_void_the_hand():
    """The regression that matters most.

    An earlier revision required the next expected number and voided on
    anything ahead of it, which turned ordinary out-of-order delivery into
    a dead hand across tests/test_convergence_chaos.py. Deliver 1 before 0
    and the hand must survive.
    """
    bus = RecordingBus()
    _, sessions, order = make_table(2, bus=bus)
    for cid in order:
        sessions[cid].begin_hand(hand_no=1, button=0)
    bus.drain()

    victim = sessions[order[1]]
    assert not victim.hand_voided, "precondition: hand is alive"

    ahead = dict(_first_hostless_from(bus, order[0]))
    ahead["author_seq"] = 99          # far ahead of anything seen
    victim.handle_message(order[0], ahead)

    assert not victim.hand_voided, (
        "a number arriving out of order voided the hand; reordering is "
        "legitimate and only the timeout machinery may act on a hole")


def test_a_hole_is_reported_rather_than_acted_on():
    """Sequencing must still make suppression VISIBLE."""
    bus = RecordingBus()
    _, sessions, order = make_table(2, bus=bus)
    for cid in order:
        sessions[cid].begin_hand(hand_no=1, button=0)
    bus.drain()

    victim = sessions[order[1]]
    hand = 1
    victim._author_seq_seen[(hand, 0)] = {AUTHOR_SEQ_START,
                                          AUTHOR_SEQ_START + 2}
    holes = victim.author_seq_holes(hand, 0)
    assert holes == [AUTHOR_SEQ_START + 1], (
        f"expected the missing number to be reported, got {holes}")
    assert not victim.hand_voided, "observing a hole must not void the hand"


def test_a_complete_stream_reports_no_holes():
    """The inverse: an in-order stream must not look like suppression."""
    bus = RecordingBus()
    _, sessions, order = make_table(2, bus=bus)
    for cid in order:
        sessions[cid].begin_hand(hand_no=1, button=0)
    bus.drain()

    victim = sessions[order[1]]
    for seat in range(2):
        assert victim.author_seq_holes(1, seat) == [], (
            f"an honest hand reported a hole for seat {seat}")


def test_an_unauthenticated_sender_cannot_move_a_seats_stream():
    """A stranger must not be able to desynchronise a real seat.

    If an unauthorized message consumed a number, anyone could burn the
    next value in a seat's stream and make that seat's real message look
    like a replay.
    """
    bus = RecordingBus()
    _, sessions, order = make_table(3, bus=bus)
    for cid in order:
        sessions[cid].begin_hand(hand_no=1, button=0)
    bus.drain()

    victim = sessions[order[2]]
    hand = 1
    before = set(victim._author_seq_seen.get((hand, 0), set()))

    # peer1 delivering a message that claims seat 0.
    forged = dict(_first_hostless_from(bus, order[0]))
    forged["author_seq"] = 4242
    victim.handle_message(order[1], forged)

    after = set(victim._author_seq_seen.get((hand, 0), set()))
    assert after == before, (
        "a message delivered by a peer that does not own the claimed seat "
        f"altered that seat's stream: {before} -> {after}")


def test_a_future_hand_message_survives_being_buffered_and_replayed():
    """The buffer feeds messages back through handle_message.

    _hand_msg_ok parks a future-hand message and _replay_buffer re-submits
    it once that hand begins. A receive gate that recorded on the first
    pass would see the second as a duplicate and drop it -- losing exactly
    the early key_announce the buffer exists to preserve.
    """
    bus = RecordingBus()
    _, sessions, order = make_table(2, bus=bus)
    for cid in order:
        sessions[cid].begin_hand(hand_no=1, button=0)
    bus.drain()

    late = sessions[order[1]]
    early = dict(_first_hostless_from(bus, order[0]))
    early["hand"] = 2                      # a hand this peer has not begun
    early["author_seq"] = AUTHOR_SEQ_START

    late.handle_message(order[0], early)
    assert not late._author_seq_seen.get((2, 0)), (
        "a future-hand message was recorded before its hand began; it will "
        "be dropped as a duplicate when the buffer replays it")
    assert late._msg_buffer, "the future-hand message was not buffered"
