"""Heartbeat and silence timeout, in one process against raw sockets.

Before this, a peer counted as gone only when its socket closed. A machine
that was paused, suspended, or cut off at the router sends no FIN, so the
connection stayed open to TCP and the table waited forever on a turn that
would never come.

These tests drive the real transport with a plain socket as the peer, so
what is asserted is what goes on the wire: the frame a peer receives, the
frame it may send without the game ever seeing it, and the drop it earns
by saying nothing. The constants are shrunk by monkeypatch -- the shipped
5 s / 20 s values are verified by the process tests in
test_heartbeat_processes.py, which pause a real peer.
"""
from __future__ import annotations

import gc
import json
import socket
import struct
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p import transport as T
from holdem.p2p import wire
from lifecycle import transport_threads, wait_until


@pytest.fixture(autouse=True)
def _clean_transport():
    """Every test starts and ends with the transport fully stopped."""
    T.stop()
    T.reset_callbacks()
    wait_until(lambda: not transport_threads(), timeout=3.0)
    gc.collect()
    yield
    T.stop()
    T.reset_callbacks()
    wait_until(lambda: not transport_threads(), timeout=3.0)
    gc.collect()


# ----------------------------------------------------------- raw wire helpers

def _host_port() -> int:
    """start_host on an ephemeral port; return the port to dial on loopback."""
    address = T.start_host(0)
    return int(address.rsplit(":", 1)[1])


def _framed(envelope: dict) -> bytes:
    body = json.dumps(envelope, separators=(",", ":")).encode()
    return struct.pack(">I", len(body)) + body


def _signed(mtype: str, payload: dict) -> bytes:
    """A frame this process signs with its own identity, as a peer would."""
    return _framed(json.loads(wire.pack(mtype, payload)))


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    chunks = []
    have = 0
    while have < count:
        chunk = sock.recv(count - have)
        if not chunk:
            raise AssertionError(f"peer closed after {have} of {count} bytes")
        chunks.append(chunk)
        have += len(chunk)
    return b"".join(chunks)


def _recv_frame(sock: socket.socket) -> bytes:
    """One length-prefixed frame body, still unverified."""
    length = struct.unpack(">I", _recv_exactly(sock, 4))[0]
    assert length <= T.MAX_MSG, f"frame claims {length} bytes"
    return _recv_exactly(sock, length)


# ---------------------------------------------------------------- the defaults

def test_shipped_constants_are_the_contracted_ones():
    """A shrunk constant committed by accident is a silent regression: the
    feature would still pass every test here and drop nobody in the field,
    or drop everybody."""
    assert T.HEARTBEAT_INTERVAL == 5.0
    assert T.SILENCE_TIMEOUT == 20.0
    assert T.SILENCE_TIMEOUT >= 3 * T.HEARTBEAT_INTERVAL, \
        "the timeout must survive losing a heartbeat or two"


# ------------------------------------------------------------- heartbeat out

def test_peer_receives_signed_heartbeats_on_schedule(monkeypatch):
    """Each connection writes a verifiable heartbeat every interval.

    The timing assertion is a lower bound only: three beats cannot arrive
    in less than two intervals after the first, however fast the machine
    is. An upper bound is enforced by the socket timeout, so a heartbeat
    that never comes fails the read rather than hanging the suite.
    """
    monkeypatch.setattr(T, "HEARTBEAT_INTERVAL", 0.2)
    monkeypatch.setattr(T, "SILENCE_TIMEOUT", None)
    port = _host_port()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.settimeout(5.0)
        started = time.monotonic()
        frames = [_recv_frame(client) for _ in range(3)]
        elapsed = time.monotonic() - started

    verified = [wire.unpack(raw) for raw in frames]
    assert [m["type"] for m in verified] == [T.HEARTBEAT_TYPE] * 3, \
        "a peer received something other than heartbeats on an idle socket"
    assert all(m["payload"] == {} for m in verified), \
        f"the heartbeat carries a payload: {[m['payload'] for m in verified]}"
    assert elapsed >= 0.4, \
        f"three heartbeats arrived in {elapsed:.3f}s, faster than the schedule"


def test_heartbeat_task_is_tracked_and_cancelled_with_the_connection():
    """The writer task has an owner, so shutdown can cancel it and a
    failure inside it reaches the session rather than vanishing."""
    port = _host_port()
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        assert wait_until(lambda: any(t.get_name().startswith("heartbeat-")
                                      for t in T.active_tasks()),
                          timeout=3.0), \
            f"no tracked heartbeat task: {[t.get_name() for t in T.active_tasks()]}"
    finally:
        client.close()
    assert wait_until(lambda: not any(t.get_name().startswith("heartbeat-")
                                      for t in T.active_tasks()),
                      timeout=3.0), \
        "the heartbeat outlived the connection it was proving alive"


# -------------------------------------------------------------- heartbeat in

def test_inbound_heartbeat_reaches_no_callback_but_chat_does(monkeypatch):
    """The game never sees a heartbeat.

    Both frames are sent back to back on one connection and delivery is
    serialized through a single dispatch consumer, so the chat arriving is
    proof the heartbeat was already dropped rather than merely slower.
    """
    monkeypatch.setattr(T, "HEARTBEAT_INTERVAL", None)
    monkeypatch.setattr(T, "SILENCE_TIMEOUT", None)
    seen: list = []
    T.on_message(lambda cid, msg: seen.append(msg.get("type")))
    port = _host_port()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(_signed(T.HEARTBEAT_TYPE, {}))
        client.sendall(_signed("chat", {"text": "hi"}))
        assert wait_until(lambda: seen, timeout=5.0), \
            "the chat never reached an on_message callback"
        wait_until(lambda: T.dispatch_depth() == 0, timeout=2.0)
    assert seen == ["chat"], f"a heartbeat was delivered to the game: {seen}"


# ------------------------------------------------------------ silence timeout

def test_silent_peer_is_dropped_with_exactly_one_disconnect(monkeypatch):
    """A connection that says nothing is closed through the normal path.

    The client here holds its socket open throughout: nothing but the
    silence timeout can account for the drop.
    """
    monkeypatch.setattr(T, "HEARTBEAT_INTERVAL", None)
    monkeypatch.setattr(T, "SILENCE_TIMEOUT", 0.5)
    dropped: list = []
    T.on_disconnect(dropped.append)
    port = _host_port()
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        assert wait_until(lambda: T.peer_ids(), timeout=3.0), \
            "the connection was never registered"
        conn_id = T.peer_ids()[0]
        assert wait_until(lambda: dropped, timeout=5.0), \
            "a silent peer was never dropped"
        assert dropped == [conn_id], \
            f"expected one disconnect for {conn_id}, got {dropped}"
        assert not wait_until(lambda: len(dropped) > 1, timeout=1.0), \
            f"on_disconnect fired more than once: {dropped}"
        assert not T.peer_ids(), \
            f"the dropped peer is still registered: {T.peer_ids()}"
        client.settimeout(3.0)
        assert client.recv(4096) == b"", \
            "the peer was deregistered but its socket was left open"
    finally:
        client.close()


def test_a_sent_frame_resets_the_silence_timeout(monkeypatch):
    """Any verified frame is proof of life, not just a heartbeat."""
    monkeypatch.setattr(T, "HEARTBEAT_INTERVAL", None)
    monkeypatch.setattr(T, "SILENCE_TIMEOUT", 0.6)
    dropped: list = []
    T.on_disconnect(dropped.append)
    port = _host_port()
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        assert wait_until(lambda: T.peer_ids(), timeout=3.0)
        deadline = time.monotonic() + 1.8     # three timeouts' worth
        while time.monotonic() < deadline:
            client.sendall(_signed("chat", {"text": "alive"}))
            assert not dropped, \
                "a peer sending chat was dropped for silence"
            time.sleep(0.2)
        # Then stop talking: the same connection must now be dropped.
        assert wait_until(lambda: dropped, timeout=5.0), \
            "the timeout never fired once the peer went quiet"
    finally:
        client.close()


# ------------------------------------------------------------- TCP keepalive

def test_keepalive_is_enabled_on_both_sockets_of_a_pair(monkeypatch):
    """Keepalive is the kernel-level backstop behind the heartbeat, and it
    must be set on the accepted socket as well as the dialled one."""
    monkeypatch.setattr(T, "HEARTBEAT_INTERVAL", None)
    monkeypatch.setattr(T, "SILENCE_TIMEOUT", None)
    port = _host_port()
    T.connect(f"127.0.0.1:{port}")
    assert wait_until(lambda: len(T.peer_ids()) == 2, timeout=5.0), \
        f"expected both ends registered, got {T.peer_ids()}"
    with T._writers_lock:
        writers = list(T._writers.values())
    socks = [w.get_extra_info("socket") for w in writers]
    assert all(s is not None for s in socks), "a writer exposed no socket"
    for sock in socks:
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1, \
            "SO_KEEPALIVE is off on a peer socket"


def test_keepalive_failure_does_not_lose_the_connection(monkeypatch):
    """A platform that refuses an option must cost a log line, not a peer."""
    monkeypatch.setattr(T, "HEARTBEAT_INTERVAL", None)
    monkeypatch.setattr(T, "SILENCE_TIMEOUT", None)

    class _Refusing:
        def get_extra_info(self, name, default=None):
            return _Socket() if name == "socket" else default

    class _Socket:
        def setsockopt(self, *args):
            raise OSError("setsockopt refused")

    T._enable_keepalive("conn", _Refusing())      # must not raise
