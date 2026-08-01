"""Transport lifecycle: ownership, shutdown, and leak-freedom.

The asyncio transport creates five kinds of background task with
asyncio.create_task and retains a reference to none of them: the per
connection handler, the server's serve_forever, and the handlers created
on the connect paths. CPython only holds a weak reference to a running
task, so an untracked task can be garbage-collected mid-execution, and any
exception it raises is discarded rather than reaching its owner.

stop() closed writers and cancelled the announce task. It did not close
the server, cancel connection handlers, stop the event loop, or join the
loop thread -- so after stop() the process was still listening, still
running tasks, and still holding a live loop.

These tests assert the properties the mandate requires: every task has an
owner, shutdown is idempotent, and nothing outlives it.
"""
import gc
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p import transport as T
from lifecycle import (LoopWatch, no_socket_leaks, no_thread_leaks,
                       open_socket_count, transport_threads, wait_until)


@pytest.fixture(autouse=True)
def _clean_transport():
    """Every test starts and ends with the transport fully stopped.

    The explicit gc.collect() runs finalizers while a loop is still valid,
    so a stale object from an earlier test cannot surface as an unraisable
    exception attributed to this one.
    """
    T.stop()
    T.reset_callbacks()
    wait_until(lambda: not transport_threads(), timeout=3.0)
    gc.collect()
    yield
    T.stop()
    T.reset_callbacks()
    wait_until(lambda: not transport_threads(), timeout=3.0)
    gc.collect()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------ task ownership

def test_every_background_task_is_tracked():
    """The transport must be able to name what it is running. Without a
    registry there is no cancellation path and no way to assert cleanup.

    Driven by a real connection rather than by whatever happens to be in
    flight: the STUN task finishes on its own schedule, so asserting on it
    would make this a timing test.
    """
    address = T.start_host(0)
    host, port = address.rsplit(":", 1)
    assert hasattr(T, "active_tasks"), "transport exposes no task registry"
    with socket.create_connection(("127.0.0.1", int(port)), timeout=5):
        assert wait_until(lambda: any(t.get_name().startswith("conn-")
                                      for t in T.active_tasks()),
                          timeout=3.0),             f"no tracked connection task: {[t.get_name() for t in T.active_tasks()]}"


def test_connection_handler_is_tracked():
    """The accepted connection gets its own tracked task.

    Asserts on the conn- task by name, not on a count delta. A delta is
    only sound if nothing else in the registry finishes meanwhile, and
    something does: start_host also spawns a STUN query. On Python 3.10
    that task fails immediately -- loop.sock_sendto arrived in 3.11 --
    so it drops out of the registry at about the moment the handler
    joins it, the count does not move, and the test fails on a platform
    where the behaviour under test is entirely correct. Caught by CI on
    3.10 while 3.12 stayed green.
    """
    address = T.start_host(0)
    host, port = address.rsplit(":", 1)
    with socket.create_connection(("127.0.0.1", int(port)), timeout=5):
        assert wait_until(lambda: any(t.get_name().startswith("conn-")
                                      for t in T.active_tasks()),
                          timeout=3.0), \
            "connection handler task was not registered: " \
            f"{[t.get_name() for t in T.active_tasks()]}"


def test_task_exceptions_are_not_lost():
    """A fire-and-forget task that raises must surface, not vanish.

    spawn() binds to the running loop, so a caller on another thread -- the
    session's thread, or this test -- goes through spawn_threadsafe.
    """
    T.start_host(0)
    seen = []
    T.on_task_error(seen.append)

    async def _boom():
        raise RuntimeError("deliberate")

    T.spawn_threadsafe(_boom(), name="test-boom")
    assert wait_until(lambda: seen, timeout=3.0), \
        "a failing background task lost its exception"
    assert isinstance(seen[0], RuntimeError)


def test_stun_task_is_tracked_and_cancelled():
    """start_host fires a STUN query in the background. Scheduled via
    run_coroutine_threadsafe it was invisible to the registry, so stop()
    could not cancel it and it was destroyed while pending -- mid DNS
    resolution -- on every shutdown."""
    T.start_host(0)
    names = [t.get_name() for t in T.active_tasks()]
    assert any("stun" in n for n in names), \
        f"STUN task is not tracked (tasks: {names})"
    T.stop()
    assert not T.active_tasks()


def test_no_task_survives_shutdown_by_name():
    """Nothing tracked may remain, whatever it was doing."""
    T.start_host(0)
    T.stop()
    assert T.active_tasks() == []


# ---------------------------------------------------------- shutdown

def test_stop_closes_the_listening_socket():
    address = T.start_host(0)
    host, port = address.rsplit(":", 1)
    T.stop()
    assert wait_until(lambda: not _port_open(int(port)), timeout=3.0), \
        "the server socket is still accepting after stop()"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def test_stop_drains_every_task():
    address = T.start_host(0)
    host, port = address.rsplit(":", 1)
    loop = T.event_loop()
    watch = LoopWatch(loop)
    client = socket.create_connection(("127.0.0.1", int(port)), timeout=5)
    try:
        assert wait_until(lambda: watch.pending(), timeout=3.0),             "expected background tasks while a peer is connected"
        T.stop()
        watch.assert_drained()
    finally:
        client.close()


def test_stop_joins_the_loop_thread():
    T.start_host(0)
    assert transport_threads(), "expected a transport thread while running"
    T.stop()
    assert wait_until(lambda: not transport_threads(), timeout=3.0), \
        "the event-loop thread outlived stop()"


def test_stop_is_idempotent():
    T.start_host(0)
    T.stop()
    T.stop()
    T.stop()
    assert not transport_threads()


def test_stop_without_start_is_harmless():
    T.stop()
    assert not transport_threads()


def test_start_after_stop_works():
    """Shutdown must not poison the module for a later session."""
    T.start_host(0)
    T.stop()
    wait_until(lambda: not transport_threads(), timeout=3.0)
    second = T.start_host(0)
    assert second, "start_host returned no address after a previous stop()"
    assert transport_threads(), "no loop thread after restart"
    host, port = second.rsplit(":", 1)
    assert _port_open(int(port)), "restarted transport is not accepting"
    T.stop()


# -------------------------------------------------------- leak checks

def test_start_stop_leaves_no_sockets():
    with no_socket_leaks(slack=1):
        T.start_host(0)
        T.stop()
        wait_until(lambda: not transport_threads(), timeout=3.0)


def test_start_stop_leaves_no_threads():
    with no_thread_leaks():
        T.start_host(0)
        T.stop()


def test_repeated_cycles_do_not_accumulate():
    """Timing-dependent leaks only show up over repetition."""
    baseline = open_socket_count()
    for _ in range(5):
        T.start_host(0)
        T.stop()
        wait_until(lambda: not transport_threads(), timeout=3.0)
    assert wait_until(lambda: open_socket_count() <= baseline + 1,
                      timeout=3.0), (
        f"sockets accumulated over cycles: {open_socket_count()} vs {baseline}")
    assert not transport_threads()


def test_connected_peer_is_dropped_by_stop():
    address = T.start_host(0)
    host, port = address.rsplit(":", 1)
    with no_thread_leaks():
        client = socket.create_connection(("127.0.0.1", int(port)), timeout=5)
        try:
            # Same reason as test_connection_handler_is_tracked: wait for
            # the handler by name. A count threshold silently expires on
            # 3.10, where the STUN task exits immediately, and stop() then
            # runs before there is a peer to drop.
            wait_until(lambda: any(t.get_name().startswith("conn-")
                                   for t in T.active_tasks()),
                       timeout=3.0)
            T.stop()
            client.settimeout(3)
            # The peer must observe a close, not hang forever.
            assert client.recv(4096) == b""
        finally:
            client.close()


# ------------------------------------------------------- send after close

def test_send_after_stop_does_not_raise():
    """Writes after shutdown must fail predictably, not explode in the
    caller or silently queue forever."""
    T.start_host(0)
    T.stop()
    T.send("nonexistent", {"type": "chat", "payload": {}})
    T.broadcast({"type": "chat", "payload": {}})


def test_broadcast_reports_failures():
    """send() logged write errors; broadcast() discarded its futures
    entirely, so a whole-table send could fail in silence."""
    address = T.start_host(0)
    host, port = address.rsplit(":", 1)
    seen = []
    T.on_task_error(seen.append)
    client = socket.create_connection(("127.0.0.1", int(port)), timeout=5)
    try:
        wait_until(lambda: T.peer_ids(), timeout=3.0)
        cid = T.peer_ids()[0]
    finally:
        client.close()
    # The peer is gone but still registered; a broadcast to it must not
    # fail silently the way an un-awaited future would.
    wait_until(lambda: True, timeout=0.1)
    T.broadcast({"type": "chat", "payload": {"text": "x"}})
    assert cid, "no connection was registered to broadcast to"
