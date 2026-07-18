"""
P2P transport -- asyncio TCP + LAN multicast rendezvous + STUN/relay.

py-libp2p failed to install (fastecdsa build error), so this module
implements a compatible asyncio TCP fallback.  The interface is identical
to what a libp2p backend would expose; swapping the backend only requires
changing this file.

Public API
----------
start_host(port=0) -> str
    Bind a TCP server on an ephemeral port.  Returns "host:port".
    Also fires off STUN discovery in the background — call
    get_public_address() after a few seconds to retrieve the result.

connect(address: str) -> str
    Connect to "host:port" with a 3-second timeout.  If *address* starts
    with ``relay://host:port/room_code``, routes through the relay instead.
    Returns a conn_id string.

connect_via_relay(relay_host, relay_port, room_code) -> str
    Open a relayed connection through the room-based proxy server.
    Returns a conn_id string (transparent after the join handshake).

get_public_address() -> tuple[str, int] | None
    Return the STUN-discovered (public_ip, public_port), or None if STUN
    has not completed or failed.

set_relay_address(host, port)
    Record the fallback relay server address for this session.

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
Sprint 3A adds STUN hole-punching and relay fallback.  The host calls
start_host() which automatically queries stun.l.google.com in the
background.  The STUN result is embedded in the invite code so joiners
can attempt a direct TCP connection; if that fails within 3 seconds they
transparently fall back to the relay at 192.168.1.10:7878.
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
_loop_lock = threading.Lock()          # H-2: guards _loop initialisation

# conn_id -> asyncio StreamWriter
_writers: dict[str, asyncio.StreamWriter] = {}
_writers_lock = threading.Lock()

# callbacks (set before starting)
_msg_callbacks:   list[Callable] = []
_conn_callbacks:  list[Callable] = []
_disc_callbacks:  list[Callable] = []

# listen address returned by start_host()
_listen_address: str = ""

# STUN-discovered public address, set asynchronously after start_host()
_public_address: Optional[tuple[str, int]] = None

# Relay server address (set by set_relay_address before generating invite code)
_relay_address: Optional[tuple[str, int]] = None

# multicast constants
_MC_GROUP = "239.255.77.77"
_MC_PORT  = 7777
_MC_TTL   = 1   # LAN-only; one hop

# announce loop task handle (so we can cancel it)
_announce_task: Optional[asyncio.Task] = None

# C-3: Maximum allowed message size (1 MB) to prevent OOM DoS
MAX_MSG = 1 << 20  # 1 048 576 bytes

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


def get_public_address() -> Optional[tuple[str, int]]:
    """Return the STUN-discovered ``(public_ip, public_port)``, or ``None``.

    This is populated asynchronously after ``start_host()`` is called.
    Returns ``None`` if STUN has not yet completed or if it failed.
    """
    return _public_address


def set_relay_address(host: str, port: int) -> None:
    """Record the fallback relay server for this session.

    Called by the host after ``start_host()``; the address is embedded in
    the invite code so joiners can use it when direct TCP fails.
    """
    global _relay_address
    _relay_address = (host, port)


def reset_callbacks() -> None:
    """Clear all registered callbacks.

    Call before re-registering callbacks for a new session to prevent
    accumulation when the lobby dialog is opened more than once (fix for
    stale-callback finding in the audit).
    """
    _msg_callbacks.clear()
    _conn_callbacks.clear()
    _disc_callbacks.clear()


# ---------------------------------------------------------------------------
# Wire framing: 4-byte big-endian length + JSON bytes
# ---------------------------------------------------------------------------

def _frame(msg: dict) -> bytes:
    body = json.dumps(msg, separators=(",", ":")).encode()
    return struct.pack(">I", len(body)) + body


# Control frames exchanged with the *relay server* (not peers) are the only
# messages that legitimately travel unsigned: they are addressed to the relay
# itself, never dispatched to the game session, and carry no game authority.
_RELAY_CONTROL_TYPES = frozenset({"relay_join"})


def _sign_frame(msg: dict) -> bytes:
    """Wrap a peer-bound dict in a signed envelope, then frame it.

    C-1: every peer-to-peer message is signed centrally here, so no call
    site can accidentally send an unsigned game or control message. Frames
    that already carry a signature (pre-packed by a caller) pass through
    unchanged; relay-control frames are framed without signing.
    """
    if "sig" in msg or msg.get("type") in _RELAY_CONTROL_TYPES:
        return _frame(msg)
    from holdem.p2p import wire as _wire
    signed = json.loads(_wire.pack(msg.get("type", ""), msg.get("payload", {})))
    return _frame(signed)


async def _read_msg(reader: asyncio.StreamReader) -> dict:
    header = await reader.readexactly(4)
    length = struct.unpack(">I", header)[0]
    # C-3: reject oversized frames before allocating memory
    if length > MAX_MSG:
        raise ValueError(f"oversized frame: {length} bytes (max {MAX_MSG})")
    body = await reader.readexactly(length)
    # M-1: propagate JSON errors as ValueError so _handle_connection can close cleanly
    try:
        msg = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON frame: {exc}") from exc
    # C-1: every peer message MUST be a valid signed envelope. Relay-control
    # frames are the sole exception (addressed to the relay, not a session).
    if msg.get("type") in _RELAY_CONTROL_TYPES:
        return msg
    from holdem.p2p import wire as _wire
    return _wire.unpack(body)   # raises ValueError on missing/bad/expired sig


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
    except ValueError as exc:
        # C-1/C-3/M-1: bad signature, oversized frame, or malformed JSON → drop peer
        log.warning("transport: dropping conn %s: %s", conn_id, exc)
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
    """Bind a TCP listener.  Returns the listen address as 'host:port'.

    Also schedules a STUN binding request in the background so that
    ``get_public_address()`` eventually returns the NAT-facing address.
    The STUN query does not block this call.
    """
    global _listen_address, _public_address
    _ensure_loop()

    # Reset any leftover STUN result from a previous session
    _public_address = None

    fut: asyncio.Future = asyncio.run_coroutine_threadsafe(
        _start_server(port), _loop
    )
    _listen_address = fut.result(timeout=10)

    # Fire STUN in background — does not block the Tk main thread
    asyncio.run_coroutine_threadsafe(_resolve_stun(), _loop)

    return _listen_address


async def _resolve_stun() -> None:
    """Query STUN and store the result in ``_public_address``."""
    global _public_address
    try:
        local_port_str = _listen_address.rsplit(":", 1)[-1]
        local_port = int(local_port_str)
    except (ValueError, IndexError):
        local_port = 0
    try:
        from holdem.p2p import stun as _stun
        _public_address = await _stun.get_public_address(local_port)
        log.info("STUN resolved: %s:%d", *_public_address)
    except Exception as exc:
        log.warning("STUN failed: %s", exc)
        _public_address = None


def _get_lan_ip() -> str:
    """Return the host's non-loopback IPv4 LAN address (H-1).

    Tries getaddrinfo first for a real interface address; falls back to
    gethostbyname; falls back to 127.0.0.1 so the app never crashes.
    """
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                return ip
    except OSError:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


async def _start_server(port: int) -> str:
    async def _accept(reader, writer):
        addr = writer.get_extra_info("peername", ("unknown", 0))
        cid = _new_conn_id()
        # L-5: use create_task instead of deprecated ensure_future
        asyncio.create_task(
            _handle_connection(reader, writer, cid, f"{addr[0]}:{addr[1]}")
        )

    server = await asyncio.start_server(_accept, "0.0.0.0", port)
    actual_port = server.sockets[0].getsockname()[1]
    # Keep the server running in the background
    asyncio.create_task(server.serve_forever())
    # H-1: announce the real LAN IP, not the unroutable 0.0.0.0
    lan_ip = _get_lan_ip()
    return f"{lan_ip}:{actual_port}"


def connect(address: str) -> str:
    """Connect to a peer.  Returns the conn_id.

    *address* forms:
    - ``"host:port"``     — direct TCP, 3-second asyncio timeout.
    - ``"relay://host:port/room_code"`` — connect through relay server.

    Raises ``ConnectionError`` on failure so callers can implement their
    own fallback (e.g. ``connect_via_relay``).
    """
    _ensure_loop()
    if address.startswith("relay://"):
        # relay://host:port/room_code
        rest = address[len("relay://"):]
        host_port, _, room_code = rest.partition("/")
        relay_host, relay_port_s = host_port.rsplit(":", 1)
        return connect_via_relay(relay_host, int(relay_port_s), room_code)

    host, port_s = address.rsplit(":", 1)
    fut = asyncio.run_coroutine_threadsafe(
        _connect_direct(host, int(port_s)), _loop
    )
    return fut.result(timeout=10)


async def _connect_direct(host: str, port: int,
                           timeout: float = 3.0) -> str:
    """Open a direct TCP connection with *timeout* seconds."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, OSError) as exc:
        raise ConnectionError(
            f"Direct TCP connect to {host}:{port} failed: {exc}"
        ) from exc
    cid = _new_conn_id()
    asyncio.create_task(  # L-5
        _handle_connection(reader, writer, cid, f"{host}:{port}")
    )
    return cid


# Keep legacy name for any internal callers
_connect_to = _connect_direct


def connect_via_relay(relay_host: str, relay_port: int,
                      room_code: str) -> str:
    """Connect through the room-based relay server.

    Sends ``{"type": "relay_join", "room": room_code, "peer_id": ...}``
    and then treats the TCP stream as a normal peer connection (same
    length-prefixed JSON framing).

    Raises ``ConnectionError`` when the relay is unreachable.
    """
    _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(
        _connect_via_relay(relay_host, relay_port, room_code), _loop
    )
    return fut.result(timeout=15)


async def _connect_via_relay(relay_host: str, relay_port: int,
                              room_code: str) -> str:
    """Async implementation of ``connect_via_relay``."""
    from holdem.p2p import identity as _identity

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(relay_host, relay_port),
            timeout=5.0,
        )
    except (asyncio.TimeoutError, OSError) as exc:
        raise ConnectionError(
            f"Relay {relay_host}:{relay_port} unreachable: {exc}"
        ) from exc

    # Send the relay join handshake
    join_msg = {
        "type":    "relay_join",
        "room":    room_code,
        "peer_id": _identity.peer_id(),
    }
    try:
        writer.write(_frame(join_msg))
        await writer.drain()
    except OSError as exc:
        writer.close()
        raise ConnectionError(f"Relay handshake failed: {exc}") from exc

    cid = _new_conn_id()
    asyncio.create_task(
        _handle_connection(
            reader, writer, cid, f"relay:{relay_host}:{relay_port}"
        )
    )
    log.info("transport: relay connection established (%s) room=%s",
             cid, room_code)
    return cid


def send(conn_id: str, msg: dict) -> None:
    """Send *msg* to the peer identified by *conn_id*."""
    _ensure_loop()
    # M-3: store future and attach an exception callback so failures are logged
    fut = asyncio.run_coroutine_threadsafe(_send_to(conn_id, msg), _loop)
    fut.add_done_callback(
        lambda f: log.warning("transport.send(%s) error: %s", conn_id, f.exception())
        if not f.cancelled() and f.exception() else None
    )


async def _send_to(conn_id: str, msg: dict) -> None:
    with _writers_lock:
        writer = _writers.get(conn_id)
    if writer is None:
        log.warning("transport.send: no writer for conn_id %s", conn_id)
        return
    try:
        writer.write(_sign_frame(msg))   # C-1: sign every peer-bound message
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


def disconnect(conn_id: str) -> None:
    """Close the connection for a specific peer (e.g. kick from host)."""
    _ensure_loop()
    with _writers_lock:
        writer = _writers.get(conn_id)
    if writer is not None:
        _loop.call_soon_threadsafe(writer.close)


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

    # H-3: schedule the coroutine from *inside* the asyncio thread so we get a
    # real asyncio.Task (not a concurrent.futures.Future), making cancel() reliable.
    async def _schedule():
        return asyncio.create_task(_loop_announce())

    fut = asyncio.run_coroutine_threadsafe(_schedule(), _loop)
    _announce_task = fut.result(timeout=5)  # now a real asyncio.Task


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
    """Start the background asyncio event loop thread if not already running.

    H-2: the entire body is held under _loop_lock so that two threads cannot
    both pass the "is None" check and each create a new event loop.
    """
    global _loop, _thread
    with _loop_lock:
        if _loop is not None and not _loop.is_closed():
            return
        _loop = asyncio.new_event_loop()

        def _run():
            asyncio.set_event_loop(_loop)
            _loop.run_forever()

        _thread = threading.Thread(target=_run, daemon=True, name="p2p-transport")
        _thread.start()
