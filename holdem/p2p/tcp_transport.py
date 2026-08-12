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
import logging
import socket
import threading
import time

from holdem.p2p import wire

_log = logging.getLogger(__name__)

# Longest single newline-terminated line accepted from a peer. Reading
# until a newline with no bound lets a peer that never sends one grow the
# buffer until the process dies -- and in the handshake that is reachable
# before any authentication.
MAX_LINE = wire.MAX_JSON_BYTES


class SimpleTcpTransport:
    """JSONL TCP transport for two-process tests.  One peer per instance."""

    #: This transport does not sign (see the module docstring), so what it
    #: delivers carries no verified author. Session reads this to choose
    #: AUTHOR_MODE_COMPAT, where seat authority falls back to the delivering
    #: connection because there is no author to check against.
    delivers_verified_envelopes = False

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
        # Read theirs, bounded. An unbounded accumulate-until-newline lets a
        # peer that never sends one grow this buffer without limit, before
        # any authentication has happened.
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise EOFError("peer closed during handshake")
            buf += chunk
            if len(buf) > MAX_LINE:
                raise ValueError(
                    f"handshake exceeded {MAX_LINE} bytes without a newline")
        hello_line, _, leftover = buf.partition(b"\n")
        peer_hello = wire.safe_loads(hello_line)
        if not isinstance(peer_hello, dict):
            raise ValueError("handshake is not a JSON object")
        peer_id = peer_hello.get("conn_id")
        if not isinstance(peer_id, str) or not peer_id:
            raise ValueError("handshake carried no usable conn_id")
        sock.settimeout(None)

        with self._writers_lock:
            self._peers[peer_id] = sock

        self._connected.set()
        # ``leftover`` is everything recv() pulled in past the hello's
        # newline. It was previously discarded, and the reader then opened a
        # fresh makefile on the socket -- so a peer whose first frame shared
        # a read with its handshake had that frame silently destroyed. The
        # peer stayed registered and the reader stayed alive, waiting for
        # bytes that had already been consumed, which is why the resulting
        # failure looked like a delivery timeout with everything healthy.
        self._start_reader(peer_id, sock, leftover)

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

    def broadcast_except(self, exclude_conn_id: str, msg: dict) -> None:
        """Send to all connected peers except one -- the relay seam."""
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        with self._writers_lock:
            socks = [s for cid, s in self._peers.items()
                     if cid != exclude_conn_id]
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

    def _start_reader(self, peer_id: str, sock: socket.socket,
                      leftover: bytes = b"") -> None:
        """Serve ``peer_id``, starting from any bytes the handshake over-read.

        ``leftover`` is bounded by the handshake, which refuses more than
        MAX_LINE before a newline, so seeding the loop with it cannot be
        used to bypass the per-line cap below.
        """
        def _read() -> None:
            f = sock.makefile("rb")
            carry = leftover
            try:
                while True:
                    # Bounded: readline(MAX_LINE + 1) caps a single line, so
                    # a peer that never sends a newline cannot grow this
                    # without limit. A truncated read means the line was
                    # over-long, which is a protocol violation, not a frame
                    # to skip -- keeping the connection would resynchronise
                    # mid-message and interpret payload bytes as a frame.
                    if b"\n" in carry:
                        line, _, carry = carry.partition(b"\n")
                        raw = line + b"\n"
                    else:
                        # Whatever the handshake over-read counts against
                        # this line's budget, so the cap still applies to
                        # the line as a whole rather than per read.
                        budget = MAX_LINE + 1 - len(carry)
                        chunk = f.readline(budget) if budget > 0 else b""
                        if not chunk:
                            break                   # peer closed
                        raw = carry + chunk
                        carry = b""
                    if not raw:
                        break                       # peer closed
                    if len(raw) > MAX_LINE and not raw.endswith(b"\n"):
                        _log.warning(
                            "tcp: %s sent a line over %d bytes - dropping peer",
                            peer_id, MAX_LINE)
                        break
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        msg = wire.safe_loads(raw)
                    except ValueError as exc:
                        # Malformed peer input is a protocol event, not a
                        # local error: log it and keep serving the peer.
                        _log.warning("tcp: %s sent an undecodable frame: %s",
                                     peer_id, exc)
                        continue
                    if not isinstance(msg, dict):
                        _log.warning("tcp: %s sent a non-object frame", peer_id)
                        continue
                    if self._session is not None:
                        self._session.handle_message(peer_id, msg)
            except OSError as exc:
                _log.debug("tcp: reader for %s ended: %s", peer_id, exc)
            except Exception:
                # A bug in message handling must not vanish. Previously this
                # swallowed everything, so one unexpected exception killed
                # the reader and left the peer connected but permanently
                # deaf, with no protocol outcome recorded anywhere.
                _log.exception("tcp: reader for %s failed", peer_id)
            finally:
                try:
                    f.close()
                except OSError:
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
