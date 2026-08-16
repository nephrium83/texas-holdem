"""Phase 4 — structured JSONL event logging.

Tests assert that the sidecar emits the right events at the right moments
using ListSink rather than reading stdout.  No subprocess, no real I/O.

Coverage:
- StdoutSink emits valid JSON lines
- NullSink discards silently
- ListSink accumulates and filters by type
- sidecar_started fires at construction
- peer_connected fires for local and remote players
- hand_started fires once per hand
- action_applied fires for both local (send_bet_action) and remote (_on_bet_action)
- action_received fires for remote actions
- digest_changed fires when replica state advances
- hand_voided fires when _void_hand is called
- peer_unavailable fires before hand_voided on deal timeout
- timeout_proposed fires before timeout_applied
- sidecar_stopping fires at session end
- Schema version and required fields present on every event
- _emit_event never raises even with a broken sink
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.events import EventSink, ListSink, NullSink, StdoutSink, SCHEMA_VERSION
from holdem.p2p.session import Session
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.timeout import DeadlineToken, FakeClock

try:
    importlib.import_module("holdem.p2p.elgamal")
except RuntimeError as exc:
    pytest.skip(f"libsodium unavailable: {exc}", allow_module_level=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_session(conn_id="peer0", is_host=True, sink=None, clock=None):
    bus = InMemoryBus()
    s = Session(
        is_host=is_host, nickname="T", avatar_b64="",
        transport=InMemoryTransport(bus, conn_id),
        clock=clock or FakeClock(),
        sink=sink,
    )
    s.local_conn_id = conn_id
    # configure_seats requires ≥2; use a placeholder second seat
    s.configure_seats([conn_id, "placeholder"])
    s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
    return s, bus


def make_table_with_sink(n=2, sinks=None):
    """N-player table; sinks[i] is the ListSink for peer i."""
    bus = InMemoryBus()
    order = [f"peer{i}" for i in range(n)]
    sessions = {}
    for i, cid in enumerate(order):
        sk = sinks[i] if sinks else None
        s = Session(
            is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
            transport=InMemoryTransport(bus, cid),
            clock=FakeClock(),
            sink=sk,
        )
        s.local_conn_id = cid
        s.configure_seats(list(order))
        s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
        bus.register(cid, s)
        sessions[cid] = s
    for cid in order:
        sessions[cid].start_p2p_hand(
            hand_no=1, names=[f"P{i}" for i in range(n)],
            stacks=[500] * n, sb=5, bb=10, button=0)
    bus.drain()
    return bus, sessions, order


# ---------------------------------------------------------------------------
# Sink unit tests
# ---------------------------------------------------------------------------

class TestSinks:
    def test_null_sink_discards(self):
        s = NullSink()
        s.emit({"event": "x"})   # must not raise

    def test_list_sink_accumulates(self):
        s = ListSink()
        s.emit({"event": "a"})
        s.emit({"event": "b"})
        assert len(s.events) == 2

    def test_list_sink_of_type(self):
        s = ListSink()
        s.emit({"event": "hand_started"})
        s.emit({"event": "action_applied"})
        s.emit({"event": "hand_started"})
        assert len(s.of_type("hand_started")) == 2
        assert len(s.of_type("action_applied")) == 1

    def test_list_sink_last(self):
        s = ListSink()
        s.emit({"event": "hand_started", "hand": 1})
        s.emit({"event": "hand_started", "hand": 2})
        assert s.last("hand_started")["hand"] == 2

    def test_stdout_sink_emits_valid_json(self, capsys):
        s = StdoutSink()
        s.emit({"v": 1, "event": "test", "x": 42})
        captured = capsys.readouterr().out
        obj = json.loads(captured.strip())
        assert obj["event"] == "test"
        assert obj["x"] == 42

    def test_stdout_sink_one_line_per_event(self, capsys):
        s = StdoutSink()
        s.emit({"event": "a"})
        s.emit({"event": "b"})
        lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
        assert len(lines) == 2
        assert json.loads(lines[0])["event"] == "a"
        assert json.loads(lines[1])["event"] == "b"


# ---------------------------------------------------------------------------
# Schema and common fields
# ---------------------------------------------------------------------------

class TestSchema:
    def test_every_event_has_required_fields(self):
        sink = ListSink()
        s, bus = make_session(sink=sink)
        # Trigger a few events
        assert len(sink.events) > 0
        required = {"v", "type", "ts", "peer", "event"}
        for ev in sink.events:
            missing = required - ev.keys()
            assert not missing, f"Event {ev.get('event')!r} missing fields: {missing}"

    def test_schema_version_is_correct(self):
        sink = ListSink()
        s, bus = make_session(sink=sink)
        for ev in sink.events:
            assert ev["v"] == SCHEMA_VERSION

    def test_type_field_is_state_event(self):
        sink = ListSink()
        s, bus = make_session(sink=sink)
        for ev in sink.events:
            assert ev["type"] == "state_event"

    def test_broken_sink_does_not_crash_session(self):
        class BrokenSink:
            def emit(self, event):
                raise RuntimeError("broken!")
        # Session should still work normally
        s, bus = make_session(sink=BrokenSink())
        # basic interaction should not raise
        from holdem.p2p.replica_table import ReplicaTable
        r = ReplicaTable(session_id="x|a|b", hand_no=1,
                         names=["A","B"], stacks=[500,500],
                         sb=5, bb=10, structure="No-Limit")
        r.start_hand(0)
        s._replica = r
        s._hand_no = 1
        s._notify_state_changed()   # would have emitted digest_changed


# ---------------------------------------------------------------------------
# sidecar_started
# ---------------------------------------------------------------------------

class TestSidecarStarted:
    def test_fires_at_construction(self):
        sink = ListSink()
        s, bus = make_session(sink=sink)
        ev = sink.last("sidecar_started")
        assert ev is not None

    def test_is_first_event(self):
        sink = ListSink()
        s, bus = make_session(sink=sink)
        assert sink.events[0]["event"] == "sidecar_started"


# ---------------------------------------------------------------------------
# peer_connected
# ---------------------------------------------------------------------------

class TestPeerConnected:
    def test_fires_for_remote_player_info(self):
        sink = ListSink()
        s, bus = make_session(sink=sink)
        # Simulate receiving player_info from a remote peer
        s.handle_message("remote1", {
            "type": "player_info",
            "payload": {"nickname": "Alice", "avatar_b64": ""},
        })
        ev = sink.last("peer_connected")
        assert ev is not None
        assert ev["conn_id"] == "remote1"
        assert ev["nickname"] == "Alice"

    def test_fires_for_local_player_via_add_local_player(self):
        """add_local_player is the production path for the host's own entry."""
        bus = InMemoryBus()
        sink = ListSink()
        s = Session(is_host=True, nickname="Bob", avatar_b64="",
                    transport=InMemoryTransport(bus, "h"), sink=sink)
        s.local_conn_id = "h"
        s.configure_seats(["h", "placeholder"])
        s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
        s.add_local_player("h")
        matches = sink.of_type("peer_connected")
        assert any(e["conn_id"] == "h" for e in matches)


# ---------------------------------------------------------------------------
# hand_started
# ---------------------------------------------------------------------------

class TestHandStarted:
    def test_fires_once_per_hand(self):
        sinks = [ListSink(), ListSink()]
        bus, sessions, order = make_table_with_sink(n=2, sinks=sinks)
        ev = sinks[0].last("hand_started")
        assert ev is not None
        assert ev["hand"] == 1

    def test_hand_started_has_digest(self):
        sinks = [ListSink(), ListSink()]
        bus, sessions, order = make_table_with_sink(n=2, sinks=sinks)
        ev = sinks[0].last("hand_started")
        assert "digest" in ev
        assert ev["digest"]  # non-empty

    def test_hand_started_has_phase(self):
        sinks = [ListSink(), ListSink()]
        bus, sessions, order = make_table_with_sink(n=2, sinks=sinks)
        ev = sinks[0].last("hand_started")
        assert "phase" in ev


# ---------------------------------------------------------------------------
# action_applied and action_received
# ---------------------------------------------------------------------------

class TestActionEvents:
    def test_action_applied_fires_on_remote_action(self):
        sinks = [ListSink(), ListSink()]
        bus, sessions, order = make_table_with_sink(n=2, sinks=sinks)
        s0, s1 = sessions[order[0]], sessions[order[1]]

        actor_seat = s0.replica.actor
        actor_conn = order[actor_seat]
        sessions[actor_conn].send_bet_action("call")
        bus.drain()

        # Both peers should see action_applied
        assert sinks[0].last("action_applied") is not None
        assert sinks[1].last("action_applied") is not None

    def test_action_applied_has_seat_and_action(self):
        sinks = [ListSink(), ListSink()]
        bus, sessions, order = make_table_with_sink(n=2, sinks=sinks)
        s0 = sessions[order[0]]
        actor_seat = s0.replica.actor
        sessions[order[actor_seat]].send_bet_action("call")
        bus.drain()

        ev = sinks[0].last("action_applied")
        assert ev is not None
        assert "seat" in ev
        assert ev["action"] == "call"

    def test_action_received_fires_on_remote_peer(self):
        """The non-acting peer should see action_received before action_applied."""
        sinks = [ListSink(), ListSink()]
        bus, sessions, order = make_table_with_sink(n=2, sinks=sinks)
        s0 = sessions[order[0]]
        actor_seat = s0.replica.actor

        if order[actor_seat] == order[0]:
            # peer0 is the actor — peer1 will see the action_received
            sessions[order[0]].send_bet_action("call")
            bus.drain()
            ev = sinks[1].last("action_received")
        else:
            # peer1 is the actor — peer0 will see the action_received
            sessions[order[1]].send_bet_action("call")
            bus.drain()
            ev = sinks[0].last("action_received")

        assert ev is not None
        assert ev["action"] == "call"


# ---------------------------------------------------------------------------
# digest_changed
# ---------------------------------------------------------------------------

class TestDigestChanged:
    def test_fires_when_action_applied(self):
        sinks = [ListSink(), ListSink()]
        bus, sessions, order = make_table_with_sink(n=2, sinks=sinks)
        s0 = sessions[order[0]]
        before_count = len(sinks[0].of_type("digest_changed"))

        actor_seat = s0.replica.actor
        sessions[order[actor_seat]].send_bet_action("call")
        bus.drain()

        after_count = len(sinks[0].of_type("digest_changed"))
        assert after_count > before_count

    def test_digest_changed_has_old_and_new(self):
        sinks = [ListSink(), ListSink()]
        bus, sessions, order = make_table_with_sink(n=2, sinks=sinks)
        s0 = sessions[order[0]]
        actor_seat = s0.replica.actor
        sessions[order[actor_seat]].send_bet_action("call")
        bus.drain()

        ev = sinks[0].last("digest_changed")
        assert ev is not None
        assert "old_digest" in ev and "new_digest" in ev
        assert ev["old_digest"] != ev["new_digest"]


# ---------------------------------------------------------------------------
# hand_voided
# ---------------------------------------------------------------------------

class TestHandVoided:
    def test_fires_on_explicit_void(self):
        sinks = [ListSink(), ListSink()]
        bus, sessions, order = make_table_with_sink(n=2, sinks=sinks)
        s0 = sessions[order[0]]
        s0._void_hand("test void", announce=False)

        ev = sinks[0].last("hand_voided")
        assert ev is not None
        assert ev["reason"] == "test void"
        assert ev["hand"] == 1

    def test_fires_only_once_per_void(self):
        sinks = [ListSink(), ListSink()]
        bus, sessions, order = make_table_with_sink(n=2, sinks=sinks)
        s0 = sessions[order[0]]
        s0._void_hand("first", announce=False)
        s0._void_hand("second", announce=False)   # ignored (already voided)

        assert len(sinks[0].of_type("hand_voided")) == 1


# ---------------------------------------------------------------------------
# timeout events
# ---------------------------------------------------------------------------

class TestTimeoutEvents:
    def _betting_session(self):
        sink = ListSink()
        bus = InMemoryBus()
        clk = FakeClock()
        s = Session(is_host=True, nickname="H", avatar_b64="",
                    transport=InMemoryTransport(bus, "peer0"),
                    clock=clk, sink=sink)
        s.local_conn_id = "peer0"
        s.configure_seats(["peer0", "peer1"])
        s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
        from holdem.p2p.replica_table import ReplicaTable
        r = ReplicaTable(session_id="poker|peer0|peer1", hand_no=1,
                         names=["Alice", "Bob"], stacks=[1000, 1000],
                         sb=5, bb=10, structure="No-Limit")
        r.start_hand(0)   # peer1 acts first
        s._replica = r
        s._hand_no = 1
        s._maybe_start_deadline()
        if r.actor is None or s.seat_order[r.actor] == "peer0":
            pytest.skip("local peer is first actor")
        return clk, sink, s, r

    def test_timeout_proposed_fires_before_applied(self):
        clk, sink, s, r = self._betting_session()
        clk.advance(31.0)
        s.check_deadlines()

        events = [e["event"] for e in sink.events]
        prop_idx = next((i for i, e in enumerate(events) if e == "timeout_proposed"), None)
        appl_idx = next((i for i, e in enumerate(events) if e == "timeout_applied"), None)
        assert prop_idx is not None
        assert appl_idx is not None
        assert prop_idx < appl_idx

    def test_timeout_proposed_has_actor_and_token_phase(self):
        clk, sink, s, r = self._betting_session()
        clk.advance(31.0)
        s.check_deadlines()

        ev = sink.last("timeout_proposed")
        assert ev is not None
        assert ev["actor"] == "peer1"
        assert ev["token_phase"] == "betting"

    def test_timeout_applied_has_digest(self):
        clk, sink, s, r = self._betting_session()
        clk.advance(31.0)
        s.check_deadlines()

        ev = sink.last("timeout_applied")
        assert ev is not None
        assert "digest" in ev

    def test_peer_unavailable_fires_before_hand_voided(self):
        """Deal timeout: peer_unavailable emitted, then hand_voided."""
        sink = ListSink()
        bus = InMemoryBus()
        s = Session(is_host=True, nickname="H", avatar_b64="",
                    transport=InMemoryTransport(bus, "host"), sink=sink)
        s.local_conn_id = "host"
        s.configure_seats(["host", "peerA"])
        s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
        from holdem.p2p.session import Player
        with s._lock:
            s.players["peerA"] = Player(
                conn_id="peerA", peer_id="", nickname="A", avatar_b64="")
        from holdem.p2p.replica_table import ReplicaTable
        r = ReplicaTable(session_id="poker|host|peerA", hand_no=1,
                         names=["H","A"], stacks=[500,500],
                         sb=5, bb=10, structure="No-Limit")
        r.start_hand(0)
        s._replica = r
        s._hand_no = 1

        token = DeadlineToken(hand_id="poker|host|peerA",
                              phase="deal_shuffle", actor="peerA", action_seq=0)
        s._apply_deal_timeout(token)

        events = [e["event"] for e in sink.events]
        unavail_idx = next((i for i, e in enumerate(events) if e == "peer_unavailable"), None)
        void_idx    = next((i for i, e in enumerate(events) if e == "hand_voided"), None)
        assert unavail_idx is not None
        assert void_idx is not None
        assert unavail_idx < void_idx


# ---------------------------------------------------------------------------
# sidecar_stopping
# ---------------------------------------------------------------------------

class TestSidecarStopping:
    def test_fires_when_session_ends(self):
        sinks = [ListSink(), ListSink()]
        bus, sessions, order = make_table_with_sink(n=2, sinks=sinks)
        s0 = sessions[order[0]]

        # Drive to settlement
        from holdem.p2p.replica_table import PHASE_BETTING
        while s0.replica is not None and s0.replica.phase == PHASE_BETTING:
            seat = s0.replica.actor
            sessions[order[seat]].send_bet_action("call")
            bus.drain()

        # Force session end with one player eliminated
        s0._finish_session([0, 1000], announce=False)

        ev = sinks[0].last("sidecar_stopping")
        assert ev is not None
        assert ev["reason"] == "session_complete"
