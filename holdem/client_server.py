"""Localhost client server -- the socket layer between a Session and its
rendering client (the Godot front end).

Implements GODOT_PROTOCOL.md: newline-delimited JSON over a localhost TCP
socket (section 2), hello + initial snapshot on connect (section 7),
command -> command_result + fresh snapshot (section 4), and an unprompted
snapshot push whenever the session reports state change (section 5), via
Session.on_state_changed. Message bodies come verbatim from
holdem.client_view -- this module adds transport only, no game logic.

Thread-safety: on_state_changed may fire on the transport's thread; the
push is marshalled onto the server's event loop with call_soon_threadsafe
and coalesced per client (a burst of changes yields one snapshot of the
latest state, which is all a renderer needs).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from holdem import client_view

_log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1


class ClientServer:
    """Serves one local Session to rendering clients over localhost TCP.

    One sidecar process = one seat = one Session = one ClientServer.
    Multiple client connections are allowed (a reconnect may briefly
    overlap the old socket); each gets its own hello + snapshot stream.
    """

    def __init__(self, session, host: str = "127.0.0.1", port: int = 0):
        self._session = session
        self._host = host
        self._port = port
        self._server: Optional[asyncio.AbstractServer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._conns: set[_Conn] = set()
        self._prev_hook = None

    @property
    def port(self) -> int:
        """The bound port (resolved after start() when constructed with 0)."""
        return self._port

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(
            self._accept, self._host, self._port)
        self._port = self._server.sockets[0].getsockname()[1]
        # Chain, never clobber, any state hook already installed.
        self._prev_hook = self._session.on_state_changed
        self._session.on_state_changed = self._state_changed
        _log.info("client server listening on %s:%s", self._host, self._port)

    async def stop(self) -> None:
        if self._session.on_state_changed is self._state_changed:
            self._session.on_state_changed = self._prev_hook
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for conn in list(self._conns):
            conn.close()
        await asyncio.sleep(0)          # let reader loops observe the close

    # -- state push ----------------------------------------------------

    def _state_changed(self) -> None:
        if self._prev_hook is not None:
            self._prev_hook()
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._mark_all_dirty)

    def _mark_all_dirty(self) -> None:
        for conn in self._conns:
            conn.dirty.set()

    # -- connections ---------------------------------------------------

    async def _accept(self, reader, writer) -> None:
        conn = _Conn(self._session, reader, writer)
        self._conns.add(conn)
        try:
            await conn.run()
        finally:
            self._conns.discard(conn)
            conn.close()


class _Conn:
    """One connected client: a reader loop plus a coalescing push task."""

    def __init__(self, session, reader, writer):
        self._session = session
        self._reader = reader
        self._writer = writer
        self.dirty = asyncio.Event()

    def close(self) -> None:
        try:
            self._writer.close()
        except Exception:                        # already gone: fine
            pass

    async def run(self) -> None:
        self._send({"type": "hello", "protocol": PROTOCOL_VERSION})
        self._send(client_view.snapshot(self._session))
        await self._drain()
        push = asyncio.create_task(self._push_loop())
        try:
            await self._read_loop()
        finally:
            push.cancel()
            try:
                await push
            except asyncio.CancelledError:
                pass

    async def _push_loop(self) -> None:
        """Unprompted snapshots (section 5): whenever the session flags a
        state change, send one snapshot of the LATEST state. Coalescing is
        free -- the event stays set through a burst and we build the
        snapshot only when we wake."""
        while True:
            await self.dirty.wait()
            self.dirty.clear()
            self._send(client_view.snapshot(self._session))
            await self._drain()

    async def _read_loop(self) -> None:
        while True:
            line = await self._reader.readline()
            if not line:
                return                           # client disconnected
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                _log.warning("client sent malformed JSON -- ignoring")
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "command":
                self._handle_command(msg)
                await self._drain()
            # any other type: ignored (forward compatibility, section 2)

    def _handle_command(self, msg: dict) -> None:
        """Section 4: apply, answer with command_result, then a fresh
        snapshot. The session validates exactly as it would a remote
        peer's action -- nothing from the client is trusted."""
        command = str(msg.get("command", ""))
        payload = msg.get("payload") or {}
        try:
            result = client_view.apply_command(self._session, command,
                                               payload)
        except (KeyError, TypeError, ValueError) as exc:
            result = {"type": "command_result", "command": command,
                      "ok": False, "error": str(exc)}
        self._send(result)
        # The snapshot below already reflects this command's effect, so a
        # coalesced push queued by it would be a duplicate: clear it. A
        # remote change racing in is only *scheduled* on the loop, so its
        # dirty-set runs after this block and is never lost.
        self.dirty.clear()
        self._send(client_view.snapshot(self._session))

    def _send(self, obj: dict) -> None:
        self._writer.write(json.dumps(obj).encode("utf-8") + b"\n")

    async def _drain(self) -> None:
        try:
            await self._writer.drain()
        except ConnectionError:
            pass                                 # reader loop will notice


__all__ = ["ClientServer", "PROTOCOL_VERSION"]
