"""
P2P transport -- asyncio TCP + LAN multicast rendezvous.

py-libp2p failed to install (fastecdsa build error), so this module
implements a compatible asyncio TCP fallback.  The interface is identical
to what a libp2p backend would expose; swapping the backend only requires
changing this file.

Public API
----------
start_host(port=0) -> str
    Bind a TCP server on an ephemeral port.  Returns "host:port".

connect(address: str) -> str
    Connect to "host:port".  Returns a conn_id string.

send(conn_id: str, msg: dict)
    Send a JSON message to one peer (length-prefixed framing).

broadcast(msg: dict)
    Send to all connected peers.

on_message(callback)
    Register handler: callback(conn_id, msg_dict).

on_connect(callback)
    Register handler: callback(conn_id, address).

on_disconnect(callback)
    Register handler: callback(conn_id).

stop()
    Shut down the transport.

Rendezvous (LAN multicast)
--------------------------
announce(rendezvous_key: str, address: str)
    Broadcast "I'm hosting at <address>" on the LAN multicast group
    239.255.77.77:7777, tagged by rendezvous_key.  Repeats every 2 s.

find_peer(rendezvous_key: str, timeout: float = 5.0) -> str | None
    Listen on the multicast group for up to *timeout* seconds.
    Returns the first matching address or None.

Internet play
-------------
For connections across the internet (not LAN), the host shares their
public IP + listen port manually.  The join dialog accepts a
"host:port override" field for this purpose.  See MULTIPLAYER.md
Phase 3 section 4 for the full connection establishment sequence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import struct
import threading
import time
import uuid
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state (singleton transport)
# ---------------------------------------------------------------------------

_loop: Optional[asyncio.AbstractEventLoop] = None
_thread: Optional[threading.Thread] = None

# conn_id -> asyncio StreamWriter
_writers: dict[str, asyncio.StreamWriter] = {}
_writers_lock = threading.Lock()

# callbacks (set before starting)
_msg_callbacks:   list[Callable] = []
_conn_callbacks:  list[Callable] = []
_disc_callbacks:  list[Callable] = []

# listen address returned by start_host()
_listen_address: str = ""

# multicast constants
_MC_GROUP = "239.255.77.77"
_MC_PORT  = 7777
_MC_TTL   = 1   # LAN-only; one hop

# announce loop task handle (so we can cancel it)
_announce_task: Optional[asyncio.Task] = None

# ---------------------------------------------------------------------------
# Public registration API (call before starting)
# ---------------------------------------------------------------------------

def on_message(callback: Callable) -> None:
    """Register callback(conn_id, msg_dict)."""
    _msg_callbacks.append(callback)


def on_connect(callback: Callable) -> None:
    """Register callback(conn_id, address)."""
    _conn_callbacks.append(callback)


def on_disconnect(callback: Callable) -> None:
    """Register callback(conn_id)."""
    _disc_callbacks.append(callback)


# ---------------------------------------------------------------------------
# Wire framing: 4-byte big-endian length + JSON bytes
# ---------------------------------------------------------------------------

def _frame(msg: dict) -> bytes:
    body = json.dumps(msg, separators=(",", ":")).encode()
    return struct.pack(">I", len(body)) + body


async def _read_msg(reader: asyncio.StreamReader) -> dict:
    header = await reader.readexactly(4)
    length = struct.unpack(">I", header)[0]
    body = await reader.readexactly(length)
    return json.loads(body)


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

def _new_conn_id() -> str:
    return str(uuid.uuid4())


async def _handle_connection(reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter,
                              conn_id: str,
                              address: str) -> None:
    with _writers_lock:
        _writers[conn_id] = writer
    log.debug("transport: connected %s (%s)", conn_id, address)
    for cb in _conn_callbacks:
        try:
            cb(conn_id, address)
        except Exception:
            log.exception("on_connect callback error")
    try:
        while True:
            msg = await _read_msg(reader)
            for cb in _msg_callbacks:
                try:
                    cb(conn_id, msg)
                except Exception:
                    log.exception("on_message callback error")
    except (asyncio.IncompleteReadError, ConnectionResetError, EOFError):
        pass
    finally:
        writer.close()
        with _writers_lock:
            _writers.pop(conn_id, None)
        log.debug("transport: disconnected %s", conn_id)
        for cb in _disc_callbacks:
            try:
                cb(conn_id)
            except Exception:
                log.exception("on_disconnect callback error")


# ---------------------------------------------------------------------------
# Public transport API
# ---------------------------------------------------------------------------

def start_host(port: int = 0) -> str:
    """Bind a TCP listener.  Returns the listen address as 'host:port'."""
    global _listen_address
    _ensure_loop()

    fut: asyncio.Future = asyncio.run_coroutine_threadsafe(
        _start_server(port), _loop
    )
    _listen_address = fut.result(timeout=10)
    return _listen_address


async def _start_server(port: int) -> str:
    async def _accept(reader, writer):
        addr = writer.get_extra_info("peername", ("unknown", 0))
        cid = _new_conn_id()
        asyncio.ensure_future(
            _handle_connection(reader, writer, cid, f"{addr[0]}:{addr[1]}")
        )

    server = await asyncio.start_server(_accept, "0.0.0.0", port)
    actual_port = server.sockets[0].getsockname()[1]
    # Keep the server running in the background
    asyncio.ensure_future(server.serve_forever())
    return f"0.0.0.0:{actual_port}"


def connect(address: str) -> str:
    """Connect to a peer at 'host:port'.  Returns the conn_id."""
    _ensure_loop()
    host, port_s = address.rsplit(":", 1)
    fut = asyncio.run_coroutine_threadsafe(
        _connect_to(host, int(port_s)), _loop
    )
    return fut.result(timeout=15)


async def _connect_to(host: str, port: int) -> str:
    reader, writer = await asyncio.open_connection(host, port)
    cid = _new_conn_id()
    asyncio.ensure_future(
        _handle_connection(reader, writer, cid, f"{host}:{port}")
    )
    return cid


def send(conn_id: str, msg: dict) -> None:
    """Send *msg* to the peer identified by *conn_id*."""
    _ensure_loop()
    asyncio.run_coroutine_threadsafe(_send_to(conn_id, msg), _loop)


async def _send_to(conn_id: str, msg: dict) -> None:
    with _writers_lock:
        writer = _writers.get(conn_id)
    if writer is None:
        log.warning("transport.send: no writer for conn_id %s", conn_id)
        return
    try:
        writer.write(_frame(msg))
        await writer.drain()
    except Exception:
        log.exception("transport.send error")


def broadcast(msg: dict) -> None:
    """Send *msg* to every connected peer."""
    _ensure_loop()
    with _writers_lock:
        conn_ids = list(_writers.keys())
    for cid in conn_ids:
        asyncio.run_coroutine_threadsafe(_send_to(cid, msg), _loop)


def stop() -> None:
    """Stop the transport and close all connections."""
    global _announce_task
    if _loop and not _loop.is_closed():
        if _announce_task:
            _loop.call_soon_threadsafe(_announce_task.cancel)
            _announce_task = None
        with _writers_lock:
            writers = list(_writers.values())
        for w in writers:
            _loop.call_soon_threadsafe(w.close)


# ---------------------------------------------------------------------------
# Rendezvous: LAN multicast
# ---------------------------------------------------------------------------

def announce(rendezvous_key: str, address: str) -> None:
    """Broadcast our address on the LAN multicast group every 2 seconds.

    The packet is a JSON object: {"key": rendezvous_key, "addr": address}.
    Runs until stop() is called or the process exits.
    """
    _ensure_loop()
    global _announce_task

    async def _loop_announce():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, _MC_TTL)
        payload = json.dumps({"key": rendezvous_key, "addr": address}).encode()
        try:
            while True:
                try:
                    sock.sendto(payload, (_MC_GROUP, _MC_PORT))
                except OSError:
                    pass
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass
        finally:
            sock.close()

    _announce_task = asyncio.run_coroutine_threadsafe(
        _loop_announce(), _loop
    )


def find_peer(rendezvous_key: str, timeout: float = 5.0) -> Optional[str]:
    """Listen on the LAN multicast group for up to *timeout* seconds.

    Returns the first address announced for *rendezvous_key*, or None.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except AttributeError:
        pass  # Windows doesn't have SO_REUSEPORT
    sock.bind(("", _MC_PORT))
    mreq = struct.pack("4sL", socket.inet_aton(_MC_GROUP), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(0.5)

    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                data, _ = sock.recvfrom(1024)
                msg = json.loads(data)
                if msg.get("key") == rendezvous_key:
                    return msg["addr"]
            except socket.timeout:
                pass
            except (json.JSONDecodeError, KeyError):
                pass
    finally:
        sock.close()
    return None


# ---------------------------------------------------------------------------
# Background event loop
# ---------------------------------------------------------------------------

def _ensure_loop() -> None:
    """Start the background asyncio event loop thread if not already running."""
    global _loop, _thread
    if _loop is not None and not _loop.is_closed():
        return
    _loop = asyncio.new_event_loop()

    def _run():
        asyncio.set_event_loop(_loop)
        _loop.run_forever()

    _thread = threading.Thread(target=_run, daemon=True, name="p2p-transport")
    _thread.start()
