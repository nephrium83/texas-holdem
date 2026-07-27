"""
Simple JSONL TCP transport for two-process integration testing.

NOT intended for production use. Sends plain newline-delimited JSON with no
signing, no length framing, and no reconnect logic. Its sole purpose is to
let two Python processes run one Session each and communicate via real TCP
sockets so that tests can assert on process-boundary behaviour without the
complexity of the full signed transport.

Interface mirrors InMemoryTransport:
    broadcast(msg)          -- send to all connected peers
    send(to_conn, msg)      -- send to a specific peer by conn_id
    attach(session)         -- register session for handle_message delivery

Startup:
    port = t.listen()       -- bind and accept ONE incoming connection
    t.connect(host, port)   -- connect to a listening peer

Messages are delivered to session.handle_message(from_conn, msg) on a
background thread.  Callers that need serialized delivery (no concurrent
session calls) should use a queue; see tests/peer_worker.py.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from typing import Optional


class SimpleTcpTransport:
    """JSONL TCP transport for two-process tests.  One peer per instance."""

    def __init__(self, local_conn_id: str) -> None:
        self._local_id = local_conn_id
        self._session  = None
        self._peers: dict[str, socket.socket] = {}     # conn_id -> socket
        self._writers_lock = threading.Lock()
        self._connected = threading.Event()             # fires after first peer connects

    # ------------------------------------------------------------------
    # Session attachment
    # ------------------------------------------------------------------

    def attach(self, session) -> None:
        """Register the session that receives incoming messages."""
        self._session = session

    # ------------------------------------------------------------------
    # Connection setup
    # ------------------------------------------------------------------

    def listen(self, port: int = 0) -> int:
        """Bind, accept one connection, start reader.  Returns bound port."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        actual_port: int = srv.getsockname()[1]

        def _accept_thread() -> None:
            try:
                conn, _ = srv.accept()
                srv.close()
                self._handshake(conn)
            except Exception:
                pass

        threading.Thread(target=_accept_thread, daemon=True,
                         name=f"tcp-accept-{self._local_id}").start()
        return actual_port

    def connect(self, host: str, port: int, timeout: float = 10.0) -> None:
        """Connect to a listening peer and start reader."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                sock = socket.create_connection((host, port), timeout=2.0)
                break
            except (ConnectionRefusedError, OSError):
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"Could not connect to {host}:{port} within {timeout}s")
                time.sleep(0.1)
        self._handshake(sock)

    def _handshake(self, sock: socket.socket) -> None:
        """Exchange conn_ids, then start the reader thread."""
        sock.settimeout(10.0)
        # send ours
        sock.sendall((json.dumps({"conn_id": self._local_id}) + "\n").encode())
        # read theirs
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise EOFError("peer closed during handshake")
            buf += chunk
        peer_hello = json.loads(buf.split(b"\n", 1)[0])
        peer_id: str = peer_hello["conn_id"]
        sock.settimeout(None)

        with self._writers_lock:
            self._peers[peer_id] = sock

        self._connected.set()
        self._start_reader(peer_id, sock)

    def wait_connected(self, timeout: float = 15.0) -> bool:
        """Block until at least one peer connects (or timeout)."""
        return self._connected.wait(timeout)

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    def broadcast(self, msg: dict) -> None:
        """Send to all connected peers."""
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        with self._writers_lock:
            socks = list(self._peers.values())
        for s in socks:
            try:
                s.sendall(data)
            except OSError:
                pass

    def send(self, to_conn: str, msg: dict) -> None:
        """Send to a specific peer by conn_id."""
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        with self._writers_lock:
            s = self._peers.get(to_conn)
        if s is not None:
            try:
                s.sendall(data)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Receive
    # ------------------------------------------------------------------

    def _start_reader(self, peer_id: str, sock: socket.socket) -> None:
        def _read() -> None:
            f = sock.makefile("rb")
            try:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if self._session is not None:
                        self._session.handle_message(peer_id, msg)
            except Exception:
                pass

        threading.Thread(target=_read, daemon=True,
                         name=f"tcp-reader-{peer_id}").start()

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._writers_lock:
            for s in self._peers.values():
                try:
                    s.close()
                except OSError:
                    pass
            self._peers.clear()


__all__ = ["SimpleTcpTransport"]
