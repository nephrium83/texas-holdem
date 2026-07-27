"""Tests for the public Session configuration and decision APIs (Phase 2).

Covers:
- configure_seats() validation (structural checks + active-hand guard)
- seat_order property returns a safe copy
- replica property exposes current ReplicaTable
- set_host_engine() wires the engine without private access
- Source-level guard: sidecar_launcher.py has no private Session field access
"""
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.session import Session
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport


def _make_session() -> Session:
    bus = InMemoryBus()
    return Session(
        is_host=True, nickname="Test", avatar_b64="",
        transport=InMemoryTransport(bus, "host"),
    )


# ------------------------------------------------------------------ configure_seats

class TestConfigureSeats:
    def test_sets_seat_order(self):
        s = _make_session()
        s.local_conn_id = "A"
        s.configure_seats(["A", "B"])
        assert s.seat_order == ["A", "B"]

    def test_produces_same_state_as_direct_assignment(self):
        """Public configure_seats produces identical seat_order to the
        internal assignment it replaces."""
        s1 = _make_session()
        s2 = _make_session()
        s1.local_conn_id = "A"
        s2.local_conn_id = "A"
        order = ["A", "B", "C"]
        s1._seat_order = list(order)          # legacy internal path
        s2.configure_seats(list(order))       # new public path
        assert s1.seat_order == s2.seat_order

    def test_seat_order_property_returns_copy(self):
        """Mutating the returned list must not affect internal state."""
        s = _make_session()
        s.local_conn_id = "A"
        s.configure_seats(["A", "B"])
        order = s.seat_order
        order.append("X")
        assert s.seat_order == ["A", "B"]

    def test_rejects_too_few_seats(self):
        s = _make_session()
        s.local_conn_id = "A"
        with pytest.raises(ValueError, match="at least 2"):
            s.configure_seats(["A"])

    def test_rejects_too_many_seats(self):
        s = _make_session()
        s.local_conn_id = "A"
        ten = [chr(ord("A") + i) for i in range(10)]
        with pytest.raises(ValueError, match="at most 9"):
            s.configure_seats(ten)

    def test_rejects_duplicate_ids(self):
        s = _make_session()
        s.local_conn_id = "A"
        with pytest.raises(ValueError, match="duplicate"):
            s.configure_seats(["A", "A"])

    def test_rejects_missing_local_conn_id(self):
        s = _make_session()
        s.local_conn_id = "A"
        with pytest.raises(ValueError, match="local conn_id"):
            s.configure_seats(["B", "C"])

    def test_skips_local_check_when_conn_id_unknown(self):
        """configure_seats is valid before local_conn_id is assigned."""
        s = _make_session()
        assert s.local_conn_id == ""
        s.configure_seats(["B", "C"])      # must not raise
        assert s.seat_order == ["B", "C"]

    def test_blocked_during_active_hand(self):
        """Cannot reconfigure seats while a hand is in progress."""
        import importlib
        try:
            importlib.import_module("holdem.p2p.elgamal")
        except RuntimeError:
            pytest.skip("libsodium unavailable")

        from holdem.p2p.replica_table import ReplicaTable

        s = _make_session()
        s.local_conn_id = "A"
        s._seat_order = ["A", "B"]
        # Inject an active (unsettled, unvoided) hand
        r = ReplicaTable(
            session_id="test|A|B", hand_no=1,
            names=["Alice", "Bob"], stacks=[1000, 1000],
            sb=5, bb=10, structure="No-Limit",
        )
        r.start_hand(0)
        s._replica = r
        with pytest.raises(RuntimeError, match="active hand"):
            s.configure_seats(["A", "B"])

    def test_allowed_after_hand_void(self):
        """Voided hand is not 'active'; reconfiguration is permitted."""
        import importlib
        try:
            importlib.import_module("holdem.p2p.elgamal")
        except RuntimeError:
            pytest.skip("libsodium unavailable")

        from holdem.p2p.replica_table import ReplicaTable

        s = _make_session()
        s.local_conn_id = "A"
        s._seat_order = ["A", "B"]
        r = ReplicaTable(
            session_id="test|A|B", hand_no=1,
            names=["Alice", "Bob"], stacks=[1000, 1000],
            sb=5, bb=10, structure="No-Limit",
        )
        r.start_hand(0)
        s._replica = r
        s.hand_voided = True               # mark voided
        s.configure_seats(["A", "B"])      # must not raise


# ------------------------------------------------------------------ replica property

class TestReplicaProperty:
    def test_replica_none_before_hand(self):
        s = _make_session()
        assert s.replica is None

    def test_replica_exposes_current_table(self):
        """Bot can read decision context via the public property."""
        import importlib
        try:
            importlib.import_module("holdem.p2p.elgamal")
        except RuntimeError:
            pytest.skip("libsodium unavailable")

        from holdem.p2p.replica_table import ReplicaTable

        s = _make_session()
        r = ReplicaTable(
            session_id="test|A|B", hand_no=1,
            names=["Alice", "Bob"], stacks=[1000, 1000],
            sb=5, bb=10, structure="No-Limit",
        )
        r.start_hand(0)
        s._replica = r
        # Public property reaches the same object — no private access needed
        assert s.replica is r
        assert s.replica.actor is not None


# ------------------------------------------------------------------ set_host_engine

class TestSetHostEngine:
    def test_wires_engine(self):
        s = _make_session()
        sentinel = object()
        s.set_host_engine(sentinel)
        assert s._engine is sentinel

    def test_replaces_previous_engine(self):
        s = _make_session()
        first = object()
        second = object()
        s.set_host_engine(first)
        s.set_host_engine(second)
        assert s._engine is second


# ------------------------------------------------------------------ source guards

class TestSourceGuards:
    """Launcher source must contain no private Session field access."""

    def _launcher_source(self) -> str:
        import holdem.sidecar_launcher as _mod
        return inspect.getsource(_mod)

    def test_no_private_replica_in_launcher(self):
        assert "._replica" not in self._launcher_source(), \
            "sidecar_launcher.py references ._replica"

    def test_no_private_seat_order_in_launcher(self):
        assert "._seat_order" not in self._launcher_source(), \
            "sidecar_launcher.py references ._seat_order"
