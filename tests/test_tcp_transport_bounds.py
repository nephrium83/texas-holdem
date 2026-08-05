"""Socket-level resource bounds on SimpleTcpTransport.

Both loops that read from a peer used to accumulate until they saw a
newline, with no cap. A peer that connects and never sends one grows the
buffer until the process dies -- and in the handshake that is reachable
before any authentication has happened.

These drive real sockets, because the defect is in the read loop rather
than in anything a unit test of the parser would touch.
"""
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p import wire
from holdem.p2p.tcp_transport import MAX_LINE, SimpleTcpTransport


# Waits below are outer deadlock guards, not timing assertions. The
# property under test is that a frame arrives (or that the reader survived);
# under full-suite socket load loopback delivery has been observed to exceed
# a 5s budget, so the bound is generous on purpose.
WAIT = 20

# The handshake is a different kind of wait and gets its own bound. It is
# not a deadlock guard: SimpleTcpTransport._handshake sets a 10s socket
# timeout on its own side, so a server that is going to answer at all has
# answered by then. Waiting longer here only delays the report.
_HANDSHAKE_WAIT = 10


class Recorder:
    """Stands in for a Session; records what reached handle_message."""

    def __init__(self):
        self.messages = []
        self.arrived = threading.Event()

    def handle_message(self, conn_id, msg):
        self.messages.append((conn_id, msg))
        self.arrived.set()


@pytest.fixture
def listening():
    """A transport listening on an ephemeral port, plus a raw client sock."""
    transport = SimpleTcpTransport("host")
    recorder = Recorder()
    transport.attach(recorder)
    port = transport.listen(0)
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    yield transport, recorder, client, port
    try:
        client.close()
    except OSError:
        pass
    transport.close()


def _handshake(client, conn_id="attacker"):
    """Complete the client half of the handshake, or fail saying why.

    Every caller below depends on this having SUCCEEDED. The previous
    version broke out of its read loop on a closed connection without
    checking that it had received anything, and returned the empty
    buffer. The caller then wrote a frame into a dead socket and failed
    twenty seconds later as "a legal frame never arrived" -- a delivery
    timeout standing in for a handshake that never happened, which sends
    the reader looking at transport delivery instead of at the peer that
    hung up.

    So: a connection that closes, times out, or answers with something
    the protocol does not accept fails HERE, reporting the buffer it got.
    """
    deadline = time.monotonic() + _HANDSHAKE_WAIT
    client.sendall(b'{"conn_id": "%s"}\n' % conn_id.encode())
    buf = b""
    while b"\n" not in buf:
        remaining = deadline - time.monotonic()
        assert remaining > 0, (
            f"server sent no handshake line within {_HANDSHAKE_WAIT}s; "
            f"received {len(buf)} bytes so far: {buf[:200]!r}")
        client.settimeout(remaining)
        try:
            chunk = client.recv(4096)
        except socket.timeout:
            continue                    # re-check the deadline and report
        if not chunk:
            raise AssertionError(
                "server closed the connection during the handshake after "
                f"{len(buf)} bytes: {buf[:200]!r}")
        buf += chunk

    line = buf.split(b"\n", 1)[0]
    hello = wire.safe_loads(line)
    assert isinstance(hello, dict), \
        f"server handshake line is not a JSON object: {line[:200]!r}"
    assert isinstance(hello.get("conn_id"), str) and hello["conn_id"], \
        f"server handshake carried no usable conn_id: {hello!r}"
    return buf


# ------------------------------------------------------ bounded reads

def test_handshake_without_a_newline_is_bounded(listening):
    """Pre-authentication memory exhaustion: connect, never send a newline.

    The peer must be dropped rather than buffered indefinitely. Sending
    somewhat more than the cap is enough -- if the bound were absent this
    would be limited only by how long the test is willing to write.
    """
    _transport, _rec, client, _port = listening
    blob = b"x" * (MAX_LINE // 4)
    dropped = False
    try:
        for _ in range(8):
            client.sendall(blob)
            time.sleep(0.02)
    except OSError:
        dropped = True          # server closed on us, which is the fix working
    # Either the send failed or the connection is now dead; confirm the
    # server is no longer willing to talk to this socket.
    if not dropped:
        client.settimeout(2)
        try:
            assert client.recv(4096) == b"", "server kept the over-long peer"
        except OSError:
            pass


def test_over_long_line_after_handshake_drops_the_peer(listening):
    """Same attack, post-handshake: one enormous line with no newline."""
    transport, recorder, client, _port = listening
    _handshake(client)
    try:
        for _ in range(8):
            client.sendall(b"y" * (MAX_LINE // 4))
            time.sleep(0.02)
        client.sendall(b"\n")
    except OSError:
        pass
    time.sleep(0.2)
    assert not recorder.messages, "an over-long line was delivered as a frame"


def _server_state(transport):
    """Everything observable about the server half, for failure messages.

    This test is intermittently flaky in CI (seen on 3.12 and 3.13) and
    the bare "a legal frame never arrived" says nothing about WHY. The
    three states below are distinguishable and point at different
    causes: no peer means the server-side handshake never completed and
    its exception was swallowed by _accept_thread; a registered peer
    with no live reader means the reader thread died or never started;
    both present means the frame was genuinely not delivered.
    """
    peers = sorted(transport._peers)
    readers = sorted(t.name for t in threading.enumerate()
                     if t.name.startswith("tcp-reader-") and t.is_alive())
    accepts = sorted(t.name for t in threading.enumerate()
                     if t.name.startswith("tcp-accept-") and t.is_alive())
    return (f"peers={peers} live_readers={readers} live_accepts={accepts}")


def test_normal_traffic_still_flows(listening):
    """The bounds must not break the honest path."""
    transport, recorder, client, _port = listening
    _handshake(client)
    client.sendall(b'{"type": "chat", "payload": {"text": "hi"}}\n')
    assert recorder.arrived.wait(WAIT), (
        f"a legal frame never arrived within {WAIT}s -- {_server_state(transport)}")
    assert recorder.messages[0][1]["type"] == "chat"


# --------------------------------------------------- malformed input

@pytest.mark.parametrize("raw", [
    b"not json\n",
    b"{\n",
    b'{"a":}\n',
    b"[" * 200 + b"]" * 200 + b"\n",
    b'{"a": 1e999999}\n',
    b"[1,2,3]\n",                       # valid JSON, but not an object
])
def test_malformed_frames_do_not_kill_the_reader(listening, raw):
    """Hostile input is a protocol event, not a local error: the frame is
    dropped and the peer keeps being served. Previously a blanket
    `except Exception: pass` killed the reader thread, leaving the peer
    connected but permanently deaf with no protocol outcome recorded."""
    transport, recorder, client, _port = listening
    _handshake(client)
    client.sendall(raw)
    time.sleep(0.1)
    assert not recorder.messages, "a malformed frame was delivered"

    # The reader must still be alive: a following legal frame gets through.
    client.sendall(b'{"type": "chat", "payload": {"text": "after"}}\n')
    assert recorder.arrived.wait(20), "reader died on malformed input"
    assert recorder.messages[-1][1]["payload"]["text"] == "after"


def test_deeply_nested_frame_is_rejected_not_crashed(listening):
    """The RecursionError case, end to end over a socket."""
    transport, recorder, client, _port = listening
    _handshake(client)
    client.sendall(b"[" * 20_000 + b"]" * 20_000 + b"\n")
    time.sleep(0.2)
    assert not recorder.messages
    client.sendall(b'{"type": "chat", "payload": {"text": "alive"}}\n')
    assert recorder.arrived.wait(20), "reader died on deep nesting"


# ------------------------------------------------------- handshake

@pytest.mark.parametrize("hello", [
    b"not json\n",
    b"[1,2,3]\n",
    b"{}\n",                            # no conn_id
    b'{"conn_id": 7}\n',                # wrong type
    b'{"conn_id": ""}\n',               # empty
])
def test_bad_handshake_is_refused(listening, hello):
    """A peer that cannot state a usable conn_id must not be registered."""
    transport, _rec, client, _port = listening
    client.sendall(hello)
    time.sleep(0.2)
    assert not transport._peers, f"registered a peer for {hello!r}"


def test_max_line_matches_the_json_bound():
    """One number to reason about, not two that can drift apart."""
    assert MAX_LINE == wire.MAX_JSON_BYTES
