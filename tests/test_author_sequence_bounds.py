"""Wire streams cannot allocate arbitrary sequence state or gap reports."""
import builtins

import pytest

from holdem.p2p import session as protocol, wire
from test_identity_injection import _signed_pair


def _host():
    host, _, _, _, _, _, remote = _signed_pair()
    host.configure_seats(["host", "joiner"])
    host._bind_seat_keys()
    host._hand_no = 1
    return host, remote


@pytest.mark.parametrize("value", [None, True, "0", -1, 1 << 60])
def test_remote_wire_sequence_is_required_and_bounded_before_ingress(value):
    host, remote = _host()
    payload = {"seat": 1, "hand": 1}
    if value is not None:
        payload["author_seq"] = value
    message = wire.unpack(wire.pack("key_announce", payload, signer=remote.signer, clock=remote.clock), clock=remote.clock)
    incoming = host._normalize_hostless("joiner", message)
    assert not host._admit_hostless(incoming)
    assert host._author_seq_seen == {}


def test_valid_reordered_wire_messages_still_close_the_gap():
    host, remote = _host()
    for sequence in (1, 0):
        raw = wire.pack("key_announce", {"seat": 1, "hand": 1, "author_seq": sequence},
                        signer=remote.signer, clock=remote.clock)
        ctx = host._normalize_hostless("joiner", wire.unpack(raw, clock=remote.clock))
        assert host._admit_hostless(ctx)
    assert host.author_seq_holes(1, 1) == []
    assert set(host._author_seq_seen[(1, 1)]) == {0, 1}


def test_gap_diagnostics_never_walk_an_attacker_sized_range(monkeypatch):
    host, _ = _host()
    host._author_seq_seen[(1, 1)] = {1 << 60: "untrusted historical state"}
    def bounded_range(*args):
        span = args[-1] - (args[0] if len(args) > 1 else 0)
        assert span <= 4096, "diagnostic attempted an unbounded gap scan"
        return builtins.range(*args)
    monkeypatch.setattr(protocol, "range", bounded_range, raising=False)
    assert len(host.author_seq_holes(1, 1)) <= 4096
    report = host.author_seq_hole_report(1)
    assert len(report) == 1 and report[0]["capped"] is True


@pytest.mark.parametrize("hand", [1, 2])
def test_invalid_sequence_is_dropped_before_current_or_future_state(hand):
    host, remote = _host()
    message = wire.unpack(wire.pack("key_announce", {"seat": 1, "hand": hand},
                                   signer=remote.signer, clock=remote.clock), clock=remote.clock)
    host.handle_message("joiner", message)
    assert host._msg_buffer == [] and host._author_seq_seen == {}


def test_forward_window_and_absolute_budget_do_not_allocate_rejected_entries():
    host, _ = _host()
    assert not host._author_seq_ok(1, 1, {"author_seq": protocol.AUTHOR_SEQ_LOOKAHEAD}, "ahead")
    assert host._author_seq_seen == {}
    for sequence in range(protocol.AUTHOR_SEQ_LIMIT):
        assert host._author_seq_ok(1, 1, {"author_seq": sequence}, str(sequence))
    assert not host._author_seq_ok(1, 1, {"author_seq": protocol.AUTHOR_SEQ_LIMIT}, "over budget")
    assert len(host._author_seq_seen[(1, 1)]) == protocol.AUTHOR_SEQ_LIMIT


def test_unsequenced_compatibility_is_explicit_and_local_delivery_remains_usable():
    host, remote = _host()
    assert host._admit_hostless(host._normalize_hostless("host", {
        "type": "key_announce", "seat": 0, "hand": 1}))
    host.author_mode = protocol.AUTHOR_MODE_COMPAT
    assert host._author_seq_ok(1, 1, {}, "compatibility")


def test_new_hands_retain_only_current_and_preceding_author_streams():
    from crypto_gate import initialize_for_tests
    initialize_for_tests()
    host, _ = _host()
    host._adopt_deal_policy(host.DEAL_POLICY_BG)
    host._adopt_continuation_policy(Session.CONTINUATION_END_TABLE)
    for hand in range(1, 6):
        host._author_seq_seen[(hand, 1)] = {0: "record"}
        host.begin_hand(hand)
        assert all(hand - 1 <= key[0] <= hand for key in host._author_seq_seen)
        assert all(hand - 1 <= key[0] <= hand for key in host._author_seq_out)


def test_long_legal_no_limit_hand_does_not_exhaust_the_control_budget():
    from holdem.p2p.replica_table import ReplicaTable, PHASE_BETTING
    host, _ = _host()
    host._table_cfg = {"total_chips": 40000}
    replica = ReplicaTable(session_id="long legal hand", hand_no=1,
                           names=["host", "joiner"], stacks=[20000, 20000], sb=1, bb=2)
    assert replica.start_hand(0)
    counters = [0, 0]
    for _ in range(8200):
        seat = replica.actor
        amount = replica.engine.legal(seat)["min_to"]
        assert replica.apply_action(replica.next_seq, seat, "raise", amount) == "applied"
        sequence = counters[seat]
        assert host._author_seq_ok(seat, 1, {"author_seq": sequence}, str(sequence))
        counters[seat] += 1
    assert min(counters) > protocol.AUTHOR_SEQ_LIMIT
    assert replica.phase == PHASE_BETTING


def test_gap_after_large_complete_prefix_is_still_reported():
    host, _ = _host()
    host._author_seq_seen[(1, 1)] = {n: str(n) for n in range(5002) if n != 5000}
    assert host.author_seq_holes(1, 1) == [5000]
    assert host.author_seq_hole_report(1) == [{"seat": 1, "missing": [5000]}]
