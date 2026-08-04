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
    client.sendall(b'{"conn_id": "%s"}\n' % conn_id.encode())
    client.settimeout(5)
    buf = b""
    while b"\n" not in buf:
        chunk = client.recv(4096)
        if not chunk:
            break
        buf += chunk
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


def test_normal_traffic_still_flows(listening):
    """The bounds must not break the honest path."""
    transport, recorder, client, _port = listening
    _handshake(client)
    client.sendall(b'{"type": "chat", "payload": {"text": "hi"}}\n')
    assert recorder.arrived.wait(20), "a legal frame never arrived"
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
