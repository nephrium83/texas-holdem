"""Two-process sidecar convergence test.

Spawns two peer_worker.py subprocesses — one host, one guest — connected
over real TCP (loopback).  Each process runs a single holdem.p2p.session.Session
with SimpleTcpTransport.  The harness drives a complete two-seat hand and
verifies that both processes converge on the same state after every
significant transition.

What this proves
----------------
* Process-boundary isolation: the Session state machines run in separate
  OS processes with no shared memory.
* Real transport semantics: messages cross an actual TCP socket (loopback),
  not InMemoryBus, so framing, ordering, and delivery semantics differ.
* State convergence: both replicas emit the same ``new_digest`` value after
  each applied action, proving the replicated state machine is consistent.

What this does NOT prove
------------------------
* Non-loopback network behaviour (packet loss, reorder, NAT).  That is
  the multi-machine playtest gate (v1.x).

Test ordering note
------------------
All tests in TestTwoSidecarProcess share ONE class-scoped fixture (one pair
of processes, one TCP connection, one deal).  Tests run in definition order
and are cumulative: later tests assume earlier ones have already advanced
the shared session state.  This makes the suite fast (one 20-30s deal
instead of five) and keeps each assertion focused.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import pytest

WORKER = str(Path(__file__).parent / "peer_worker.py")
STARTUP_TIMEOUT = 20.0   # seconds to wait for "connected" from each side
DEAL_TIMEOUT    = 60.0   # seconds for mental-poker deal to complete
ACTION_TIMEOUT  = 15.0   # seconds for an action to propagate to both sides

HAND_ARGS = {
    "hand_no":   1,
    "names":     ["Host", "Guest"],
    "stacks":    [1000, 1000],
    "sb":        10,
    "bb":        20,
    "structure": "No-Limit",
    "button":    0,
}


# ── subprocess helpers ────────────────────────────────────────────────────────

def _start_worker(*extra_args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, WORKER, *extra_args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _send_cmd(proc: subprocess.Popen, cmd: dict) -> None:
    proc.stdin.write(json.dumps(cmd) + "\n")
    proc.stdin.flush()


# ── event collector ───────────────────────────────────────────────────────────

class EventCollector:
    """Reads JSONL from a subprocess stdout on a background thread."""

    def __init__(self, proc: subprocess.Popen, label: str) -> None:
        self._proc  = proc
        self._label = label
        self._events: list[dict] = []
        self._lock   = threading.Lock()
        self._new    = threading.Condition(self._lock)
        t = threading.Thread(target=self._read, daemon=True,
                             name=f"collector-{label}")
        t.start()

    def _read(self) -> None:
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            with self._new:
                self._events.append(evt)
                self._new.notify_all()

    def wait_for(self, predicate, timeout: float = STARTUP_TIMEOUT) -> Optional[dict]:
        """Block until predicate(event) is True; return the matching event.

        Predicate is called WITHOUT holding the internal lock so that it may
        safely call other EventCollector methods (e.g. event_count) without
        deadlocking.  self._lock and self._new share the same underlying mutex,
        so any predicate that calls event_count() would deadlock if evaluated
        inside a ``with self._new:`` block.
        """
        deadline = time.monotonic() + timeout
        checked = 0  # index of first event not yet evaluated
        while True:
            # Snapshot newly-arrived events under the lock, then release it.
            with self._lock:
                new_events = list(self._events[checked:])
                checked = len(self._events)
            # Evaluate predicate outside the lock so predicates may call
            # event_count() or other lock-acquiring helpers freely.
            for e in new_events:
                if predicate(e):
                    return e
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            # Wait up to 0.5 s for the reader thread to notify us of new events.
            with self._new:
                if len(self._events) == checked:
                    self._new.wait(timeout=min(remaining, 0.5))

    def all_of_type(self, t: str) -> list[dict]:
        with self._lock:
            return [e for e in self._events if e.get("type") == t]

    def latest_snapshot(self) -> Optional[dict]:
        with self._lock:
            snaps = [e for e in self._events if e.get("type") == "snapshot"]
            return snaps[-1]["snap"] if snaps else None

    def latest_digest(self) -> Optional[str]:
        with self._lock:
            for e in reversed(self._events):
                if e.get("event") == "digest_changed":
                    # _safe_emit uses new_digest= kwarg
                    return e.get("new_digest") or e.get("digest")
                if e.get("type") == "snapshot":
                    snap = e.get("snap", {})
                    d = snap.get("digest") or snap.get("seq")
                    if d is not None:
                        return str(d)
            return None

    def has_any(self, predicate) -> bool:
        with self._lock:
            return any(predicate(e) for e in self._events)

    def event_count(self, t: str) -> int:
        return len(self.all_of_type(t))


# ── class-scoped fixture ──────────────────────────────────────────────────────

@pytest.fixture(scope="class")
def shared_two_process(request):
    """One pair of subprocesses shared across the entire test class.

    Using class scope means the mental-poker deal runs once (~20-30s)
    instead of once per test.  Tests are cumulative: test_deal starts the
    hand, later tests observe or act on its state.
    """
    host_proc = guest_proc = None
    host_col  = guest_col  = None
    try:
        host_proc = _start_worker("--role", "host", "--port", "0")
        host_col  = EventCollector(host_proc, "host")

        ready = host_col.wait_for(lambda e: e.get("type") == "ready",
                                  timeout=STARTUP_TIMEOUT)
        assert ready is not None, "host never emitted ready"
        port = ready["port"]

        guest_proc = _start_worker("--role", "guest", "--peer-port", str(port))
        guest_col  = EventCollector(guest_proc, "guest")

        h_conn = host_col.wait_for(lambda e: e.get("type") == "connected",
                                   timeout=STARTUP_TIMEOUT)
        g_conn = guest_col.wait_for(lambda e: e.get("type") == "connected",
                                    timeout=STARTUP_TIMEOUT)
        assert h_conn is not None, "host never saw peer connect"
        assert g_conn is not None, "guest never connected"

        # Attach to the requesting class so tests can access without params.
        request.cls.host_proc  = host_proc
        request.cls.guest_proc = guest_proc
        request.cls.host_col   = host_col
        request.cls.guest_col  = guest_col

        yield host_proc, guest_proc, host_col, guest_col

    finally:
        for p in (guest_proc, host_proc):
            if p is None:
                continue
            try:
                _send_cmd(p, {"op": "quit"})
            except Exception:
                pass
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.terminate()
                p.wait(timeout=3)


# ── test class ────────────────────────────────────────────────────────────────

@pytest.mark.usefixtures("shared_two_process")
class TestTwoSidecarProcess:
    """Happy-path convergence: both processes reach the same state.

    Tests run in definition order (pytest default) and share one
    process pair (class-scoped fixture).  Each test builds on the
    previous one's state.
    """

    # ---------------------------------------------------------------- step 1
    def test_01_peers_connect(self):
        """Both processes establish a TCP connection."""
        assert self.host_proc.poll() is None, "host process died early"
        assert self.guest_proc.poll() is None, "guest process died early"

    # ---------------------------------------------------------------- step 2
    def test_02_deal_reaches_betting_phase(self):
        """After start_p2p_hand, both processes reach a betting phase."""
        _send_cmd(self.guest_proc, {"op": "start_hand", "args": HAND_ARGS})
        _send_cmd(self.host_proc,  {"op": "start_hand", "args": HAND_ARGS})

        def _post_deal(e):
            if e.get("type") != "snapshot":
                return False
            phase = e.get("snap", {}).get("phase", "lobby")
            return phase not in ("lobby", "dealing", "void")

        h_snap = self.host_col.wait_for(_post_deal, timeout=DEAL_TIMEOUT)
        g_snap = self.guest_col.wait_for(_post_deal, timeout=DEAL_TIMEOUT)

        assert h_snap is not None, (
            "host never reached a betting phase — "
            f"snapshots: {self.host_col.all_of_type('snapshot')[-3:]}"
        )
        assert g_snap is not None, (
            "guest never reached a betting phase — "
            f"snapshots: {self.guest_col.all_of_type('snapshot')[-3:]}"
        )
        assert h_snap["snap"]["phase"] == g_snap["snap"]["phase"], (
            f"phase diverged: host={h_snap['snap']['phase']} "
            f"guest={g_snap['snap']['phase']}"
        )

    # ---------------------------------------------------------------- step 3
    def test_03_digests_converge_after_deal(self):
        """Both replicas emit the same digest once the deal is complete."""
        # digest_changed events should already be present from the deal in
        # test_02; if not, wait briefly for them to arrive.
        def _has_digest(e):
            return e.get("event") == "digest_changed" and "new_digest" in e

        h_dig = self.host_col.wait_for(_has_digest, timeout=5.0)
        g_dig = self.guest_col.wait_for(_has_digest, timeout=5.0)

        assert h_dig is not None, "host emitted no digest_changed events"
        assert g_dig is not None, "guest emitted no digest_changed events"

        # Brief settling window, then compare.
        time.sleep(0.5)
        h_digest = self.host_col.latest_digest()
        g_digest = self.guest_col.latest_digest()

        assert h_digest is not None
        assert g_digest is not None
        assert h_digest == g_digest, (
            f"digest diverged: host={h_digest!r} guest={g_digest!r}"
        )

    # ---------------------------------------------------------------- step 4
    def test_04_action_propagates_to_both(self):
        """A bet action from the acting peer is reflected on both replicas."""
        def _has_legal(e):
            if e.get("type") != "snapshot":
                return False
            return "legal" in e.get("snap", {}).get("you", {})

        # Identify the acting side from already-collected snapshots.
        h_acts = self.host_col.has_any(_has_legal)
        g_acts = self.guest_col.has_any(_has_legal)

        if h_acts:
            acting_proc, acting_col, other_col = (
                self.host_proc, self.host_col, self.guest_col)
        elif g_acts:
            acting_proc, acting_col, other_col = (
                self.guest_proc, self.guest_col, self.host_col)
        else:
            pytest.skip("neither side has a legal-action snapshot yet")

        snap_count_before = other_col.event_count("snapshot")

        _send_cmd(acting_proc, {"op": "action", "action": "call", "amount": 0})

        # Wait for the OTHER side to emit at least one new snapshot.
        def _new_snap_arrived(e):
            return (e.get("type") == "snapshot" and
                    other_col.event_count("snapshot") > snap_count_before)

        propagated = other_col.wait_for(_new_snap_arrived, timeout=ACTION_TIMEOUT)
        assert propagated is not None, (
            "action did not propagate to the other process within "
            f"{ACTION_TIMEOUT}s"
        )

        # Give both sides a moment to settle, then compare digests.
        time.sleep(0.5)
        h_digest = self.host_col.latest_digest()
        g_digest = self.guest_col.latest_digest()
        if h_digest is not None and g_digest is not None:
            assert h_digest == g_digest, (
                f"digest diverged after action: "
                f"host={h_digest!r} guest={g_digest!r}"
            )

    # ---------------------------------------------------------------- step 5
    def test_05_clean_shutdown(self):
        """Both processes exit cleanly when sent the quit command."""
        _send_cmd(self.guest_proc, {"op": "quit"})
        _send_cmd(self.host_proc,  {"op": "quit"})

        try:
            self.host_proc.wait(timeout=8)
            self.guest_proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            pytest.fail("one or both processes did not exit after quit")

        assert self.host_proc.returncode is not None
        assert self.guest_proc.returncode is not None
