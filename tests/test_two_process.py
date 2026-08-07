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
import os
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


# ── failure diagnostics ───────────────────────────────────────────────────────
#
# test_02 is intermittently flaky in CI (3.12, twice) and has never once
# reported why. The reason is structural, not incidental:
#
#   * CI sets PYTEST_TIMEOUT=60, which pytest-timeout applies to every test.
#   * test_02 performs TWO sequential wait_for calls at DEAL_TIMEOUT=60 each.
#
# So the test can burn up to 120s while the harness kills it at 60. Its own
# assertion messages -- which already try to dump the last three snapshots --
# are unreachable in CI. Every observed failure has been "Failed: Timeout
# (>60.0s) from pytest-timeout" carrying no state at all.
#
# The fix for that is not a longer timeout. It is to capture the evidence
# BEFORE the harness fires, from a watchdog that is armed when the test
# starts and disarmed when it passes. No timeout value changes here.
#
# Output goes to fd 2 directly rather than through sys.stderr, because
# pytest buffers per-test captured output and pytest-timeout's thread method
# terminates the process without flushing it. os.write to the real descriptor
# survives that; a print() does not.

_DIAG_LEAD = 12.0        # fire this many seconds before the ambient timeout
_DIAG_TAIL = 40          # events to dump per process


def _ambient_timeout() -> Optional[float]:
    """The per-test timeout pytest-timeout will enforce, if any."""
    raw = os.environ.get("PYTEST_TIMEOUT", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


_STDERR_TAIL_BYTES = 8192       # decoded tail kept for human reading
_STDERR_LOUD = 32 * 1024        # volume worth reporting even on success


class StderrSink:
    """Byte-accurate record of what a worker wrote to stderr.

    Counting LINES cannot test the pipe hypothesis. A worker can emit a
    single enormous unterminated line, push the pipe to capacity, and be
    recorded as zero lines until EOF. The quantity that matters is bytes
    against the ~64KiB pipe buffer, so bytes are what this counts.

    Reading goes through os.read on the raw descriptor rather than
    iterating the text stream, so nothing is buffered on our side and the
    count is the count the kernel saw.
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self.total = 0
        self._tail = bytearray()
        self._lock = threading.Lock()

    def feed(self, chunk: bytes) -> None:
        with self._lock:
            self.total += len(chunk)
            self._tail.extend(chunk)
            if len(self._tail) > _STDERR_TAIL_BYTES:
                del self._tail[:-_STDERR_TAIL_BYTES]

    def tail_text(self) -> str:
        with self._lock:
            return bytes(self._tail).decode("utf-8", "replace")

    @property
    def loud(self) -> bool:
        return self.total >= _STDERR_LOUD


def _drain(stream, sink: StderrSink) -> threading.Thread:
    """Continuously read a subprocess pipe into ``sink``.

    stderr was previously never read. That is why no failure has ever
    carried a worker traceback -- and it is also a live hypothesis for the
    stall itself, since a subprocess that fills the pipe nobody drains
    blocks forever on its next write.

    Draining is required to capture stderr at all, which creates a trap:
    if the stall IS a full pipe, draining cures it and the flake vanishes
    with nothing to show for it. That is why ``total`` is reported on
    SUCCESSFUL runs too, above _STDERR_LOUD. A cure with no measurement is
    a correlation; a cure alongside "host wrote 48192 bytes" is evidence.
    """
    fd = stream.fileno()

    def _pump() -> None:
        try:
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    return
                sink.feed(chunk)
        except Exception:                          # closed pipe on teardown
            pass

    t = threading.Thread(target=_pump, daemon=True,
                         name=f"stderr-drain-{sink.label}")
    t.start()
    return t


def _phases_seen(col: "EventCollector") -> list:
    """Ordered distinct phases, so a stall shows where it stopped."""
    out = []
    for e in col.all_of_type("snapshot"):
        phase = e.get("snap", {}).get("phase")
        if phase and (not out or out[-1] != phase):
            out.append(phase)
    return out


_CMD_STAGES = ("stdin_open", "stdin_line", "cmd_queued",
               "cmd_dispatch", "msg_dequeued", "msg_done")


def command_path(col) -> str:
    """Where the command path stopped, stated rather than inferred.

    peer_worker emits a trace event at each boundary. The captured CI
    failure showed the host alive with start_hand_ack=False and no
    snapshots, which is consistent with five different stalls; these
    stages tell them apart without re-reading the whole event tail:

      no stdin_open                -> reader thread never started
      stdin_open, no stdin_line    -> blocked on read, or no bytes arrived
      stdin_line, no cmd_queued    -> read but never parsed/queued
      cmd_queued, no cmd_dispatch  -> queued, consumer never reached it
      cmd_dispatch, no ack         -> dispatch began and blocked

    msg_dequeued exceeding msg_done means the single consumer is stuck
    inside session.handle_message, starving everything behind it.
    """
    if col is None:
        return "n/a"
    counts = {s: 0 for s in _CMD_STAGES}
    last = {}
    for e in list(col._events):
        if e.get("type") != "trace":
            continue
        stage = e.get("stage")
        if stage in counts:
            counts[stage] += 1
            last[stage] = e.get("t")
    parts = [f"{s}={counts[s]}" + (f"@{last[s]}s" if s in last else "")
             for s in _CMD_STAGES]
    stuck = counts["msg_dequeued"] - counts["msg_done"]
    if stuck > 0:
        parts.append(f"IN-FLIGHT handle_message={stuck}")
    return " ".join(parts)


def _side_report(label: str, proc, col, stderr_sink, t0: float,
                 t_start_hand: Optional[float]) -> str:
    now = time.monotonic()
    rc = proc.poll() if proc is not None else "no-process"
    events = list(col._events) if col is not None else []

    acks = [e for e in events if e.get("type") == "ack"]
    errors = [e for e in events if e.get("type") == "error"]
    digests = [e for e in events if e.get("event") == "digest_changed"]
    snap = col.latest_snapshot() if col is not None else None

    lines = [
        f"--- {label} " + "-" * (66 - len(label)),
        f"  process:        alive={rc is None} exit_code={rc!r}",
        f"  since_launch:   {now - t0:.1f}s",
        "  since_start_hand: "
        + ("n/a" if t_start_hand is None else f"{now - t_start_hand:.1f}s"),
        f"  events_total:   {len(events)}",
        f"  acks:           {[a.get('op') for a in acks]}",
        f"  start_hand_ack: {any(a.get('op') == 'start_hand' for a in acks)}",
        f"  phases_seen:    {_phases_seen(col) if col else []}",
        f"  command_path:   {command_path(col)}",
        f"  digest_changed: {len(digests)}",
        f"  latest_digest:  {col.latest_digest()!r}" if col else "  latest_digest:  n/a",
        f"  errors:         {errors[-3:] if errors else '[]'}",
        f"  latest_snapshot: {json.dumps(snap)[:600] if snap else 'none'}",
    ]

    lines.append(f"  event tail (last {_DIAG_TAIL}, ALL types, in order):")
    for e in events[-_DIAG_TAIL:]:
        lines.append("    " + json.dumps(e)[:300])

    total = stderr_sink.total if stderr_sink is not None else -1
    lines.append(
        f"  stderr_bytes:   {total}"
        + ("   <-- at or past the pipe-capacity danger zone"
           if total >= _STDERR_LOUD else ""))
    lines.append(f"  stderr tail (last {_STDERR_TAIL_BYTES}B decoded):")
    if stderr_sink is not None:
        for line in stderr_sink.tail_text().splitlines()[-_DIAG_TAIL:]:
            lines.append("    " + line[:300])

    return "\n".join(lines)


def _dump(reason: str, cls, waiting_for: str) -> str:
    """Full two-sided report. Never raises -- it runs on a failure path."""
    try:
        head = [
            "",
            "=" * 78,
            f"TWO-PROCESS DIAGNOSTIC: {reason}",
            f"  pending wait:  {waiting_for}",
            f"  ambient pytest timeout: {_ambient_timeout()!r}s "
            f"(DEAL_TIMEOUT={DEAL_TIMEOUT}s, used twice sequentially)",
            "=" * 78,
        ]
        body = [
            _side_report("HOST", cls.host_proc, cls.host_col,
                         getattr(cls, "host_stderr", []),
                         getattr(cls, "t0", time.monotonic()),
                         getattr(cls, "t_start_hand", None)),
            _side_report("GUEST", cls.guest_proc, cls.guest_col,
                         getattr(cls, "guest_stderr", []),
                         getattr(cls, "t0", time.monotonic()),
                         getattr(cls, "t_start_hand", None)),
            "=" * 78,
            "",
        ]
        return "\n".join(head + body)
    except Exception as exc:                       # diagnostics must not mask
        return f"\n[diagnostic failed: {exc!r}]\n"


def _emit_diag(text: str) -> None:
    try:
        os.write(2, text.encode("utf-8", "replace"))
    except Exception:
        pass


def _report_stderr_volume(host_sink: StderrSink, guest_sink: StderrSink,
                          config=None) -> None:
    """Emit stderr volume on a SUCCESSFUL run, when it is high enough to matter.

    This is the half of the instrumentation that can falsify the pipe
    hypothesis rather than merely benefit from it. If draining stderr is
    what stops the stall, then after this change CI goes green and the
    failure diagnostics never run -- so without this line, "the flake went
    away" would stay a correlation forever.

    Silent below the threshold: a healthy run that writes a few hundred
    bytes was never near capacity, and saying so on every green run would
    train people to ignore it.

    pytest's fd-level capture redirects fd 2 and DISCARDS it for passing
    tests, so a bare os.write here would be swallowed in exactly the case
    this exists to serve. Capture is suspended for the write. The failure
    path does not need this -- captured output is replayed for failures --
    which is why the watchdog controls worked before this was noticed.
    """
    if not (host_sink.loud or guest_sink.loud):
        return
    text = (f"\ntwo-process stderr volume: host={host_sink.total} bytes "
            f"guest={guest_sink.total} bytes "
            f"(threshold {_STDERR_LOUD}, pipe capacity typically 65536)\n")

    capman = None
    if config is not None:
        capman = config.pluginmanager.getplugin("capturemanager")
    if capman is None:
        _emit_diag(text)
        return
    with capman.global_and_fixture_disabled():
        _emit_diag(text)


def _arm_watchdog(cls, waiting_for: str):
    """Dump state shortly before pytest-timeout would kill the process.

    Returns a cancel callable. If there is no ambient timeout (local runs
    without PYTEST_TIMEOUT) the watchdog still arms, off DEAL_TIMEOUT, so the
    same evidence appears locally.
    """
    ambient = _ambient_timeout()
    budget = (ambient if ambient is not None else DEAL_TIMEOUT) - _DIAG_LEAD
    if budget <= 0:
        return lambda: None

    timer = threading.Timer(
        budget,
        lambda: _emit_diag(_dump(
            f"watchdog fired {budget:.0f}s in, before the harness timeout",
            cls, waiting_for)),
    )
    timer.daemon = True
    timer.start()
    return timer.cancel


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
    # stderr was never read on either process. Two consequences: no failure
    # has ever carried a worker traceback, and a worker that writes enough to
    # fill the ~64KB pipe would block forever on its next write.
    #
    # Draining it is required to capture stderr at all, so it is done here --
    # but note the trap. If the stall IS a full stderr pipe, this change makes
    # the flake disappear, and that must be reported as evidence for the
    # cause, NOT as a fix. The line counts below are what distinguish the two:
    # a run that drains tens of thousands of bytes was living dangerously; a
    # run that drains a handful was never near the limit and the pipe theory
    # is dead.
    request.cls.host_stderr = host_stderr = StderrSink("host")
    request.cls.guest_stderr = guest_stderr = StderrSink("guest")
    request.cls.t0 = time.monotonic()
    request.cls.t_start_hand = None
    try:
        host_proc = _start_worker("--role", "host", "--port", "0")
        host_col  = EventCollector(host_proc, "host")
        _drain(host_proc.stderr, host_stderr)

        ready = host_col.wait_for(lambda e: e.get("type") == "ready",
                                  timeout=STARTUP_TIMEOUT)
        assert ready is not None, "host never emitted ready"
        port = ready["port"]

        guest_proc = _start_worker("--role", "guest", "--peer-port", str(port))
        guest_col  = EventCollector(guest_proc, "guest")
        _drain(guest_proc.stderr, guest_stderr)

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
        # Before teardown, while the counts are final and the processes are
        # still up: report volume if either side was anywhere near the pipe
        # limit. This runs on passing runs too -- that is the point.
        _report_stderr_volume(host_stderr, guest_stderr, request.config)
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
        type(self).t_start_hand = time.monotonic()
        _send_cmd(self.guest_proc, {"op": "start_hand", "args": HAND_ARGS})
        _send_cmd(self.host_proc,  {"op": "start_hand", "args": HAND_ARGS})

        def _post_deal(e):
            if e.get("type") != "snapshot":
                return False
            phase = e.get("snap", {}).get("phase", "lobby")
            return phase not in ("lobby", "dealing", "void")

        # Armed before the waits, cancelled after them. Without this the
        # harness kills the test at PYTEST_TIMEOUT and none of the assertion
        # messages below ever run -- which is exactly what every observed CI
        # failure looked like.
        cancel = _arm_watchdog(
            type(self), "host+guest snapshot with phase not in "
                        "(lobby, dealing, void)")
        try:
            h_snap = self.host_col.wait_for(_post_deal, timeout=DEAL_TIMEOUT)
            g_snap = self.guest_col.wait_for(_post_deal, timeout=DEAL_TIMEOUT)
        finally:
            cancel()

        if h_snap is None or g_snap is None:
            # Processes are still up here; teardown has not run, so nothing
            # has been destroyed and no pipe has been closed yet.
            _emit_diag(_dump(
                f"host_reached={h_snap is not None} "
                f"guest_reached={g_snap is not None}",
                type(self), "post-deal snapshot"))

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
            acting_proc, _acting_col, other_col = (
                self.host_proc, self.host_col, self.guest_col)
        elif g_acts:
            acting_proc, _acting_col, other_col = (
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


# ── command-path classification ──────────────────────────────────────────────

def test_command_path_reports_a_healthy_host():
    """Control for the classifier: a working run reaches every stage.

    Without this, a classifier that silently reported nothing would look
    identical to a clean run.
    """
    proc = _start_worker("--role", "host", "--port", "0")
    col = EventCollector(proc, "host")
    sink = StderrSink("host")
    _drain(proc.stderr, sink)
    try:
        assert col.wait_for(lambda e: e.get("type") == "ready",
                            timeout=STARTUP_TIMEOUT), "host never emitted ready"
        guest = _start_worker("--role", "guest", "--peer-port",
                              str(col.all_of_type("ready")[0]["port"]))
        EventCollector(guest, "guest")   # drain stdout so it cannot block
        try:
            assert col.wait_for(lambda e: e.get("type") == "connected",
                                timeout=STARTUP_TIMEOUT)
            _send_cmd(proc, {"op": "start_hand", "args": HAND_ARGS})
            assert col.wait_for(
                lambda e: e.get("type") == "ack" and e.get("op") == "start_hand",
                timeout=STARTUP_TIMEOUT), "host never acked start_hand"
            path = command_path(col)
            for stage in ("stdin_open", "stdin_line", "cmd_queued",
                          "cmd_dispatch"):
                assert f"{stage}=0" not in path, f"{stage} never happened: {path}"
        finally:
            _send_cmd(guest, {"op": "quit"})
            guest.terminate()
    finally:
        try:
            _send_cmd(proc, {"op": "quit"})
        except Exception:
            pass
        proc.terminate()


def test_starved_consumer_is_classified_not_guessed():
    """Reproduces the captured CI shape deterministically.

    The observed failure was: host alive, stderr_bytes=0, no start_hand
    ack, no snapshots, while the guest processed the identical command.
    Five different stalls produce that, and the raw watchdog output could
    not separate them.

    Here the host's single consumer is stalled inside handle_message via
    PEER_WORKER_STALL_MS -- injection, not a fix -- while a start_hand is
    written and flushed to its stdin. The required classification is
    cmd_queued > 0 with cmd_dispatch == 0: the command arrived, was
    parsed, was queued, and the consumer never reached it.

    That is the hypothesis the CI capture pointed at, because the guest
    is told to start first and its deal traffic can fill the host's queue
    ahead of the host's own command. This test does not prove that is
    what happened in CI. It proves the instrumentation would say so.
    """
    env = dict(os.environ, PEER_WORKER_STALL_MS="8000")
    host = subprocess.Popen(
        [sys.executable, WORKER, "--role", "host", "--port", "0"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1, env=env)
    hcol = EventCollector(host, "host")
    hsink = StderrSink("host")
    _drain(host.stderr, hsink)
    guest = None
    try:
        ready = hcol.wait_for(lambda e: e.get("type") == "ready",
                              timeout=STARTUP_TIMEOUT)
        assert ready is not None
        guest = _start_worker("--role", "guest", "--peer-port",
                              str(ready["port"]))
        gcol = EventCollector(guest, "guest")
        assert hcol.wait_for(lambda e: e.get("type") == "connected",
                             timeout=STARTUP_TIMEOUT)
        assert gcol.wait_for(lambda e: e.get("type") == "connected",
                             timeout=STARTUP_TIMEOUT)

        # Guest first, exactly as test_02 does: its traffic reaches the
        # host and occupies the consumer before the host's own command.
        _send_cmd(guest, {"op": "start_hand", "args": HAND_ARGS})
        assert hcol.wait_for(
            lambda e: (e.get("type") == "trace"
                       and e.get("stage") == "msg_dequeued"),
            timeout=STARTUP_TIMEOUT), "host never began handling peer traffic"

        _send_cmd(host, {"op": "start_hand", "args": HAND_ARGS})
        assert hcol.wait_for(
            lambda e: (e.get("type") == "trace"
                       and e.get("stage") == "cmd_queued"),
            timeout=STARTUP_TIMEOUT), "start_hand never reached the queue"

        # The demonstrated state, now with a cause attached.
        acked = hcol.wait_for(
            lambda e: e.get("type") == "ack" and e.get("op") == "start_hand",
            timeout=2.0)
        assert acked is None, "consumer was not actually stalled"
        assert host.poll() is None, "host died; this is a stall test"
        assert hsink.total == 0, f"unexpected stderr: {hsink.tail_text()[:200]}"

        path = command_path(hcol)
        assert "cmd_queued=0" not in path, f"command never queued: {path}"
        assert "cmd_dispatch=0" in path, (
            f"expected a starved consumer, not a blocked dispatch: {path}")
        assert "IN-FLIGHT handle_message=" in path, (
            f"classifier did not identify the stuck consumer: {path}")
    finally:
        for p in (guest, host):
            if p is None:
                continue
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                pass
