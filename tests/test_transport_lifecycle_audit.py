"""Transport and dispatch lifecycle audit repairs (a separate file from the
pre-existing test_transport_lifecycle.py, which must not be replaced): audit
findings #55, #80, #83, #84 (see
test_transport_security), #86, #87, #88, #89 and #212."""
import asyncio
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p import transport
from holdem.p2p.dispatch import MessageWorker
from holdem.p2p.tcp_transport import MAX_LINE, SimpleTcpTransport


def _wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# ------------------------------------------------------- #55 dispatch stop

def test_stop_discards_the_backlog_instead_of_running_it():
    started, release, ran = threading.Event(), threading.Event(), []

    def handler(conn_id, msg):
        if msg["n"] == 0:
            started.set()
            release.wait(5)
        ran.append(msg["n"])

    w = MessageWorker(handler, maxsize=100)
    w.start()
    w.submit("c", {"n": 0})
    assert started.wait(5)
    for n in range(1, 50):                     # backlog behind a busy handler
        assert w.submit("c", {"n": n})
    threading.Timer(0.2, release.set).start()
    assert w.stop(timeout=5) is True
    assert ran == [0], f"queued work ran after stop(): {ran[1:6]}..."


def test_stop_reports_a_handler_that_will_not_finish():
    started, release = threading.Event(), threading.Event()

    def handler(conn_id, msg):
        started.set()
        release.wait(10)

    w = MessageWorker(handler, maxsize=4)
    w.start()
    w.submit("c", {})
    assert started.wait(5)
    try:
        assert w.stop(timeout=0.3) is False    # used to return None silently
        assert w.is_alive()
    finally:
        release.set()
        w._thread.join(5)


def test_stop_on_a_full_queue_still_returns():
    gate = threading.Event()
    w = MessageWorker(lambda c, m: gate.wait(5), maxsize=3)
    w.start()
    for _ in range(10):
        w.submit("c", {})
    threading.Timer(0.2, gate.set).start()
    assert w.stop(timeout=5) is True


def test_stop_before_start_is_a_clean_no_op():
    assert MessageWorker(lambda c, m: None).stop() is True


# ------------------------------------------- production transport fixtures

class _Writer:
    closed = False

    def get_extra_info(self, _name):
        return None

    def close(self):
        self.closed = True


@pytest.fixture
def isolated_transport(monkeypatch):
    for name in ("_writers", "_disconnect_reasons"):
        monkeypatch.setattr(transport, name, {})
    for name in ("_conn_callbacks", "_disc_callbacks", "_reason_disc_callbacks",
                 "_msg_callbacks", "_task_error_callbacks"):
        monkeypatch.setattr(transport, name, [])
    monkeypatch.setattr(transport, "HEARTBEAT_INTERVAL", None)
    monkeypatch.setattr(transport, "SILENCE_TIMEOUT", None)
    monkeypatch.setattr(transport, "_deliver_event", lambda fn, *args: fn(*args))


# ---------------------------------------- #86 one disconnect per connection

def test_a_failed_write_and_the_reader_exit_report_one_disconnect(isolated_transport):
    """A failed write drops the writer and notifies; the reader then sees
    EOF on the same socket. That used to be a second PEER_LOST."""
    lost = []
    transport.on_disconnect(lost.append)

    class _Reader:
        async def readexactly(self, n):
            transport._drop_writer("peer")     # the write path failed first
            raise asyncio.IncompleteReadError(b"", n)

    asyncio.run(transport._handle_connection(_Reader(), _Writer(), "peer", "x"))
    assert lost == ["peer"]


def test_a_plain_disconnect_still_reports_exactly_once(isolated_transport):
    lost = []
    transport.on_disconnect(lost.append)

    class _Reader:
        async def readexactly(self, n):
            raise asyncio.IncompleteReadError(b"", n)

    asyncio.run(transport._handle_connection(_Reader(), _Writer(), "peer", "x"))
    assert lost == ["peer"]


# ------------------------------------------------- #87 task-error handlers

def test_task_error_handlers_can_be_unsubscribed_and_are_reset(isolated_transport):
    seen = []
    unsubscribe = transport.on_task_error(seen.append)
    transport.on_task_error(lambda exc: seen.append("second"))
    unsubscribe()
    unsubscribe()                              # idempotent
    assert len(transport._task_error_callbacks) == 1
    transport.reset_callbacks()
    assert transport._task_error_callbacks == []


# ------------------------------------------------- #88 no resurrection

def _transport_threads():
    return [t for t in threading.enumerate()
            if t.name == "p2p-transport" and t.is_alive()]


def test_disconnect_after_stop_does_not_start_a_new_transport():
    transport.stop()
    before = len(_transport_threads())
    transport.disconnect("nobody")
    transport.disconnect("nobody", reason="cleanup")
    assert transport.event_loop() is None
    assert len(_transport_threads()) == before


# ------------------------------------------- #89 explicit stopping state

def test_a_start_during_stop_is_refused_not_mixed_in():
    with transport._loop_lock:
        transport._stopping = True
        transport._stop_done.clear()
    try:
        with pytest.raises(RuntimeError, match="stopping"):
            transport._ensure_loop()
        assert transport.event_loop() is None
    finally:
        with transport._loop_lock:
            transport._stopping = False
        transport._stop_done.set()


def test_a_second_stop_waits_for_the_first_instead_of_resetting(monkeypatch):
    resets = []
    monkeypatch.setattr(transport, "_reset_state", lambda: resets.append(1))
    with transport._loop_lock:
        transport._stopping = True
        transport._stop_done.clear()
    done = threading.Event()
    t = threading.Thread(target=lambda: (transport.stop(timeout=1), done.set()))
    t.start()
    try:
        assert not done.wait(0.3), "second stop() returned while the first ran"
        assert resets == [], "second stop() reset state mid-teardown"
    finally:
        with transport._loop_lock:
            transport._stopping = False
        transport._stop_done.set()
    assert done.wait(5)
    t.join(5)


def test_stop_clears_the_stopping_state_even_without_a_running_loop():
    transport.stop()
    transport.stop()
    assert transport._stopping is False and transport._stop_done.is_set()


# ------------------------------------ test transport (#80, #83, #212)

class _Sess:
    def __init__(self):
        self.lost, self.msgs = [], []

    def handle_disconnect(self, conn_id):
        self.lost.append(conn_id)

    def handle_message(self, conn_id, msg):
        self.msgs.append(msg)


def _pair():
    a, b = SimpleTcpTransport("A"), SimpleTcpTransport("B")
    sa, sb = _Sess(), _Sess()
    a.attach(sa)
    b.attach(sb)
    port = a.listen(0)
    b.connect("127.0.0.1", port)
    assert a.wait_connected(5) and b.wait_connected(5)
    assert _wait_until(lambda: "B" in a._peers)
    return a, b, sa, sb


def test_peer_eof_unregisters_closes_and_notifies_once():
    a, b, sa, sb = _pair()
    try:
        sock = a._peers["B"]
        b.close()
        assert _wait_until(lambda: sa.lost == ["B"])
        assert "B" not in a._peers
        assert not a._connected.is_set()
        assert sock.fileno() == -1                    # our socket was closed
        time.sleep(0.2)
        assert sa.lost == ["B"]                       # exactly once
        assert sb.lost == []                          # local close: no report
    finally:
        a.close()
        b.close()


def test_overlong_line_leaves_no_ghost_peer():
    a, b, sa, sb = _pair()
    try:
        raw = b._peers["A"]
        raw.sendall(b"x" * (MAX_LINE + 64))           # never a newline
        assert _wait_until(lambda: sa.lost == ["B"])
        assert "B" not in a._peers and not a._connected.is_set()
        a.send("B", {"type": "after"})                # no stale socket to hit
    finally:
        a.close()
        b.close()


def test_concurrent_large_sends_never_interleave_frames():
    a, b, sa, sb = _pair()
    try:
        big = "y" * 60_000
        threads = [threading.Thread(target=lambda i=i: [
            a.send("B", {"t": i, "k": k, "pad": big}) for k in range(10)])
            for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        assert _wait_until(lambda: len(sb.msgs) == 80, timeout=20), len(sb.msgs)
        assert all(m.get("pad") == big for m in sb.msgs)
        assert sorted((m["t"], m["k"]) for m in sb.msgs) == \
            sorted((i, k) for i in range(8) for k in range(10))
        assert sb.lost == []
    finally:
        a.close()
        b.close()
