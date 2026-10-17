"""The heartbeat at its shipped timings, against a really suspended peer.

The in-process tests in test_transport_heartbeat.py shrink the constants,
so they prove the mechanism and not the schedule. They also cannot
reproduce the situation the feature exists for: a machine that stops
running. A paused process has no event loop, so nothing inside it can
simulate being paused.

Here two real processes connect over loopback, each running only
``holdem.p2p.transport`` (tests/heartbeat_peer.py), and one of them is
suspended at the OS level -- SIGSTOP on POSIX, NtSuspendProcess on
Windows. A suspended process sends no FIN and answers no probe, which is
precisely what a sleeping laptop or a yanked cable looks like to its
partner, and precisely what the old "gone when the socket closes" rule
could not see.

Four tests, all at the shipped 5 s / 20 s:

  a1  pause the joiner -> the host reports disconnected within 25 s
  a2  pause the host   -> the joiner reports disconnected within 25 s
  b   pause for 10 s and resume -> nobody disconnects, and a message sent
      after the resume still arrives
  control  with --no-heartbeat on BOTH peers, a paused peer is still
      connected after 25 s -- so a1 and a2 pass because of the heartbeat
      and not because something else noticed the socket

Every wait is bounded and each test fits inside the 60 s PYTEST_TIMEOUT
that CI applies (.github/workflows/ci.yml).
"""
from __future__ import annotations

import contextlib
import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

PEER = str(Path(__file__).parent / "heartbeat_peer.py")

# Bounds. DROP_WINDOW is the 20 s silence timeout plus room for process
# startup jitter on a loaded CI runner; MIN_DROP_DELAY is the floor below
# which a "drop" would have to be something other than the timeout, since
# the earliest the timer can expire is 20 s after the last frame and the
# pause can land at most one 5 s interval after one.
READY_TIMEOUT    = 20.0
DROP_WINDOW      = 25.0
MIN_DROP_DELAY   = 10.0
SHORT_PAUSE      = 10.0
SURVIVE_WINDOW   = 5.0
MESSAGE_TIMEOUT  = 10.0


# ----------------------------------------------------------- pause / resume

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int,
                                      ctypes.c_ulong]
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _PROCESS_SUSPEND_RESUME = 0x0800

    def _nt_process_call(name: str, pid: int) -> None:
        """Freeze or thaw every thread of *pid* through the native API.

        Windows has no SIGSTOP. NtSuspendProcess is the equivalent the
        debuggers use; it is undocumented but has been present and stable
        since NT, and it is the only way to produce the state this suite
        needs -- a process that is alive, holds its socket open, and runs
        no code at all.
        """
        fn = getattr(_ntdll, name, None)
        if fn is None:                      # pragma: no cover - platform
            pytest.skip(f"ntdll does not export {name}")
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = ctypes.c_long
        handle = _kernel32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, pid)
        if not handle:
            raise OSError(ctypes.get_last_error(),
                          f"OpenProcess({pid}) for {name} failed")
        try:
            status = fn(handle)
        finally:
            _kernel32.CloseHandle(handle)
        if status != 0:
            raise OSError(f"{name}({pid}) returned {status:#010x}")

    def _suspend_process(pid: int) -> None:
        _nt_process_call("NtSuspendProcess", pid)

    def _resume_process(pid: int) -> None:
        _nt_process_call("NtResumeProcess", pid)

else:
    def _suspend_process(pid: int) -> None:
        os.kill(pid, signal.SIGSTOP)

    def _resume_process(pid: int) -> None:
        os.kill(pid, signal.SIGCONT)


# ------------------------------------------------------------------- peers

class Peer:
    """One heartbeat_peer.py subprocess and the events it has emitted.

    stdout is drained on a thread, so the peer can never block on a pipe
    nobody reads. stderr goes to a file rather than a pipe for the same
    reason -- a suspended process cannot drain anything, and a pipe that
    fills while it is frozen would change the behaviour under test.
    """

    def __init__(self, label: str, workdir: Path, *args: str) -> None:
        self.label = label
        self._suspended = False
        self._events: list[dict] = []
        self._lock = threading.Lock()
        self._stderr_path = workdir / f"{label}.stderr.txt"
        self._stderr = open(self._stderr_path, "wb")
        config_dir = workdir / f"{label}-config"
        self.proc = subprocess.Popen(
            [sys.executable, PEER, "--config-dir", str(config_dir), *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._stderr, text=True, bufsize=1)
        self._reader = threading.Thread(target=self._pump, daemon=True,
                                        name=f"heartbeat-reader-{label}")
        self._reader.start()

    # -- plumbing ---------------------------------------------------------
    def _pump(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            with self._lock:
                self._events.append(event)

    def events(self) -> list:
        with self._lock:
            return list(self._events)

    def seen(self, predicate) -> "dict | None":
        """The first recorded event matching *predicate*, or None."""
        for event in self.events():
            if predicate(event):
                return event
        return None

    def wait_for(self, predicate, timeout: float) -> "dict | None":
        """Poll the recorded events until one matches, or *timeout*."""
        deadline = time.monotonic() + timeout
        while True:
            found = self.seen(predicate)
            if found is not None:
                return found
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)

    def command(self, cmd: dict) -> None:
        self.proc.stdin.write(json.dumps(cmd) + "\n")
        self.proc.stdin.flush()

    # -- the behaviour under test ----------------------------------------
    def suspend(self) -> None:
        _suspend_process(self.proc.pid)
        self._suspended = True

    def resume(self) -> None:
        if self._suspended:
            _resume_process(self.proc.pid)
            self._suspended = False

    # -- reporting --------------------------------------------------------
    def report(self) -> str:
        tail = [json.dumps(e) for e in self.events()[-12:]]
        try:
            errors = self._stderr_path.read_text(errors="replace")[-1500:]
        except OSError:
            errors = "<unreadable>"
        return ("\n" + f"--- {self.label} (pid {self.proc.pid}, "
                f"alive={self.proc.poll() is None}) ---\n"
                + "\n".join("  " + line for line in tail)
                + f"\n  stderr tail:\n{errors}\n")

    def close(self) -> None:
        self.resume()                   # a frozen process cannot be asked to quit
        with contextlib.suppress(Exception):
            self.command({"op": "quit"})
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=5)
        with contextlib.suppress(Exception):
            self._stderr.close()


@contextlib.contextmanager
def peer_pair(workdir: Path, heartbeat: bool = True):
    """A connected host/joiner pair, torn down whatever the test did."""
    extra = [] if heartbeat else ["--no-heartbeat"]
    host = Peer("host", workdir, "--role", "host", *extra)
    joiner = None
    try:
        ready = host.wait_for(lambda e: e.get("type") == "ready",
                              READY_TIMEOUT)
        assert ready is not None, "the host never became ready" + host.report()
        if heartbeat:
            # Stated, not assumed: these tests are only about the shipped
            # schedule, and a shrunk constant would make them pass for the
            # wrong reason while proving nothing about the real build.
            assert (ready["heartbeat"], ready["silence"]) == (5.0, 20.0), \
                f"the peer is not running the shipped timings: {ready}"
        port = ready["addr"].rsplit(":", 1)[1]
        joiner = Peer("joiner", workdir, "--role", "joiner",
                      "--addr", f"127.0.0.1:{port}", *extra)
        for peer in (joiner, host):
            assert peer.wait_for(lambda e: e.get("type") == "connected",
                                 READY_TIMEOUT) is not None, \
                f"{peer.label} never saw the connection" + peer.report()
        yield host, joiner
    finally:
        for peer in (joiner, host):
            if peer is not None:
                peer.close()


def _disconnected(event: dict) -> bool:
    return event.get("type") == "disconnected"


# ------------------------------------------------------------ (a) the drop

def test_paused_joiner_is_dropped_by_the_host(tmp_path):
    """The whole point: a peer that stops running is reported gone.

    Nothing closes the joiner's socket here -- the process still holds it,
    and will go on holding it after this test resumes it. The only thing
    that has changed is that it stopped sending.
    """
    with peer_pair(tmp_path) as (host, joiner):
        paused_at = time.monotonic()
        joiner.suspend()
        drop = host.wait_for(_disconnected, DROP_WINDOW)
        elapsed = time.monotonic() - paused_at
        joiner.resume()

        assert drop is not None, (
            f"the host still believed in a peer paused {elapsed:.1f}s ago"
            + host.report() + joiner.report())
        assert elapsed >= MIN_DROP_DELAY, (
            f"the host dropped the peer after only {elapsed:.1f}s, too fast "
            f"for a {MIN_DROP_DELAY}s floor -- something other than the "
            f"silence timeout closed it" + host.report())

        host.command({"op": "peers"})
        peers = host.wait_for(lambda e: e.get("type") == "peers",
                              MESSAGE_TIMEOUT)
        assert peers is not None and peers["peers"] == [], (
            f"the dropped peer is still registered: {peers}" + host.report())


def test_paused_host_is_dropped_by_the_joiner(tmp_path):
    """The same from the other side. The host's connection is the accepted
    one rather than the dialled one, and until now only the joiner's half
    had ever been exercised by a drop."""
    with peer_pair(tmp_path) as (host, joiner):
        paused_at = time.monotonic()
        host.suspend()
        drop = joiner.wait_for(_disconnected, DROP_WINDOW)
        elapsed = time.monotonic() - paused_at
        host.resume()

        assert drop is not None, (
            f"the joiner still believed in a host paused {elapsed:.1f}s ago"
            + joiner.report() + host.report())
        assert elapsed >= MIN_DROP_DELAY, (
            f"the joiner dropped the host after only {elapsed:.1f}s"
            + joiner.report())


# ------------------------------------------------------------- (b) the blip

def test_a_short_pause_costs_nothing(tmp_path):
    """A blip must not cost a seat.

    Ten seconds is two heartbeats lost out of the four the timeout spans,
    so neither side may drop the other -- and the connection must still
    carry traffic afterwards, which is the part a liveness check can
    plausibly break by closing a socket it only suspected.
    """
    with peer_pair(tmp_path) as (host, joiner):
        joiner.suspend()
        time.sleep(SHORT_PAUSE)
        joiner.resume()

        assert host.wait_for(_disconnected, SURVIVE_WINDOW) is None, (
            f"the host dropped a peer that was only paused {SHORT_PAUSE}s"
            + host.report())
        assert joiner.seen(_disconnected) is None, (
            "the joiner dropped the host across its own pause"
            + joiner.report())

        joiner.command({"op": "send",
                        "msg": {"type": "chat",
                                "payload": {"text": "after-resume"}}})
        delivered = host.wait_for(
            lambda e: e.get("type") == "recv" and e.get("mtype") == "chat",
            MESSAGE_TIMEOUT)
        assert delivered is not None, (
            "a message sent after the resume never arrived"
            + joiner.report() + host.report())
        # The game saw the chat and nothing else: heartbeats are dropped
        # below the callback, so in a run this long there would otherwise
        # be several.
        assert host.seen(lambda e: e.get("type") == "recv"
                         and e.get("mtype") != "chat") is None, (
            "a transport frame was delivered to a message callback"
            + host.report())


# --------------------------------------------------- deliberate-break control

def test_without_the_heartbeat_a_paused_peer_is_never_dropped(tmp_path):
    """The control that makes the tests above evidence.

    With the feature off on both peers, the same pause produces no drop at
    all: the socket stays open, the host keeps the peer in its table, and
    the table would wait on it forever. If this test ever fails, the drops
    above are being caused by something other than the heartbeat and the
    whole suite is measuring the wrong thing.
    """
    with peer_pair(tmp_path, heartbeat=False) as (host, joiner):
        for peer in (host, joiner):
            ready = peer.seen(lambda e: e.get("type") == "ready")
            assert ready is not None, f"{peer.label} never became ready"
            assert ready["heartbeat"] is None and ready["silence"] is None, \
                f"{peer.label} did not start with the feature off: {ready}"

        joiner.suspend()
        try:
            assert host.wait_for(_disconnected, DROP_WINDOW) is None, (
                "the host dropped a paused peer with no heartbeat running, so "
                "the drop in the tests above is not the heartbeat's doing"
                + host.report())
            host.command({"op": "peers"})
            peers = host.wait_for(lambda e: e.get("type") == "peers",
                                  MESSAGE_TIMEOUT)
            assert peers is not None and len(peers["peers"]) == 1, (
                f"the host lost the paused peer another way: {peers}"
                + host.report())
        finally:
            joiner.resume()
