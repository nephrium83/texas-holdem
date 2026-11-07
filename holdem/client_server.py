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

from holdem.p2p.session import CommandRefused
from holdem import client_view

_log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1


class ClientServer:
    """Serves one local Session to rendering clients over localhost TCP.

    One sidecar process = one seat = one Session = one ClientServer.
    Multiple client connections are allowed (a reconnect may briefly
    overlap the old socket); each gets its own hello + snapshot stream.
    """

    def __init__(self, session, host: str = "127.0.0.1", port: int = 0,
                 start_table=None, *, snapshot_provider=None,
                 command_handler=None):
        if (snapshot_provider is None) != (command_handler is None):
            raise ValueError("snapshot_provider and command_handler must be supplied together")
        self._session = session
        self._snapshot_provider = snapshot_provider
        self._command_handler = command_handler
        self._host = host
        self._port = port
        # Controller-owned "start the configured table" callable, forwarded
        # verbatim to client_view.apply_command. This module stays transport
        # only: it neither knows nor stores what the table is configured as.
        self._start_table = start_table
        self._server: Optional[asyncio.AbstractServer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._conns: set[_Conn] = set()
        self._handlers: set[asyncio.Task] = set()
        self._stopping = False
        self._generation = 0
        self._prev_hook = None
        # Bound methods are equal but not identical on repeated attribute
        # access. Retain the exact object installed on the Session so stop()
        # can prove callback ownership before restoring the previous hook.
        self._installed_hook = self._state_changed

    @property
    def port(self) -> int:
        """The bound port (resolved after start() when constructed with 0)."""
        return self._port

    async def start(self) -> None:
        if self._server is not None or self._stopping:
            raise RuntimeError("client server already started or stopping")
        self._loop = asyncio.get_running_loop()
        self._generation += 1
        generation = self._generation
        self._server = await asyncio.start_server(
            lambda reader, writer: self._connected(reader, writer, generation),
            self._host, self._port)
        self._port = self._server.sockets[0].getsockname()[1]
        # Chain, never clobber, any state hook already installed.
        if self._session is not None:
            self._prev_hook = self._session.on_state_changed
            self._session.on_state_changed = self._installed_hook
        _log.info("client server listening on %s:%s", self._host, self._port)

    async def stop(self) -> None:
        if self._stopping:
            raise RuntimeError("client server is already stopping")
        self._stopping = True
        try:
            if (self._session is not None
                    and self._session.on_state_changed is self._installed_hook):
                self._session.on_state_changed = self._prev_hook
            server, self._server = self._server, None
            if server is not None:
                server.close()
            # Close clients before waiting for the listener: on newer asyncio
            # versions wait_closed also waits for accepted transports.
            for conn in list(self._conns):
                conn.close()
            # Tasks are registered synchronously when asyncio accepts a socket,
            # including handlers which have not entered _accept yet. Their
            # finalizers join push tasks and the writer before leaving this set.
            while self._handlers:
                await asyncio.gather(*tuple(self._handlers), return_exceptions=True)
            if server is not None:
                await server.wait_closed()
            # Let native listener cancellation finish (Windows proactor).
            # Any late connection callback is generation-fenced and closes
            # synchronously, without creating an untracked handler.
            await asyncio.sleep(0)
            self._loop = None
        finally:
            self._stopping = False

    # -- state push ----------------------------------------------------

    def _state_changed(self) -> None:
        if self._prev_hook is not None:
            try:
                self._prev_hook()
            except Exception:
                _log.exception("previous state-change hook failed")
        self.notify_state_changed()

    def notify_state_changed(self) -> None:
        """Request a snapshot from a controller callback on any thread."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            if current is loop:
                # A synchronous command will send its own updated snapshot
                # and clear this mark before yielding. Scheduling it instead
                # makes that clear run too early, causing a duplicate push.
                self._mark_all_dirty()
            else:
                loop.call_soon_threadsafe(self._mark_all_dirty)

    def _mark_all_dirty(self) -> None:
        for conn in self._conns:
            conn.dirty.set()

    # -- connections ---------------------------------------------------

    def _connected(self, reader, writer, generation) -> None:
        # Native connection_made may still be queued when an old listener
        # closes (notably on Python 3.10). Reject before creating a handler,
        # including if this server has already restarted by callback time.
        if self._stopping or self._server is None or generation != self._generation:
            writer.close()
            return
        task = asyncio.create_task(self._accept(reader, writer))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _accept(self, reader, writer) -> None:
        if self._server is None:
            writer.close()
            await writer.wait_closed()
            return
        conn = _Conn(self._session, reader, writer, self._start_table,
                     self._snapshot_provider, self._command_handler)
        self._conns.add(conn)
        try:
            await conn.run()
        finally:
            conn.close()
            try:
                await conn.wait_closed()
            finally:
                self._conns.discard(conn)


class _Conn:
    """One connected client: a reader loop plus a coalescing push task."""

    def __init__(self, session, reader, writer, start_table=None,
                 snapshot_provider=None, command_handler=None):
        self._session = session
        self._reader = reader
        self._writer = writer
        self._start_table = start_table
        self._snapshot_provider = snapshot_provider
        self._command_handler = command_handler
        self._operation = asyncio.Lock()
        self.dirty = asyncio.Event()

    def close(self) -> None:
        try:
            self._writer.close()
        except Exception:                        # already gone: fine
            pass

    async def wait_closed(self) -> None:
        try:
            await self._writer.wait_closed()
        except (AttributeError, ConnectionError):
            pass

    async def run(self) -> None:
        self._send({"type": "hello", "protocol": PROTOCOL_VERSION})
        self._send(await self._snapshot())
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
            async with self._operation:
                # Event.wait may already have awakened before the command
                # cleared its coalesced mark. Recheck under the operation
                # lock instead of emitting a second identical snapshot.
                if not self.dirty.is_set():
                    continue
                self.dirty.clear()
                self._send(await self._snapshot())
                await self._drain()

    async def _snapshot(self) -> dict:
        if self._snapshot_provider is None:
            return client_view.snapshot(self._session)
        # Waiting for a remote crypto operation's owner must not freeze the
        # localhost event loop, including its disconnect/cleanup processing.
        return await asyncio.to_thread(self._snapshot_provider)

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
                async with self._operation:
                    if self._command_handler is None:
                        self._handle_command(msg)
                    else:
                        await self._handle_controller_command(msg)
                    await self._drain()
            # any other type: ignored (forward compatibility, section 2)

    def _handle_command(self, msg: dict) -> None:
        """Section 4: apply, answer with command_result, then a fresh
        snapshot. The session validates exactly as it would a remote
        peer's action -- nothing from the client is trusted."""
        command = msg.get("command", "")
        payload = msg.get("payload")
        try:
            result = client_view.apply_command(self._session, command,
                                               payload,
                                               start_table=self._start_table)
        except (KeyError, TypeError, ValueError, CommandRefused) as exc:
            # Expected refusals are client errors; invariant RuntimeErrors escape.
            _log.exception("client command %r failed", command)
            result = {"type": "command_result", "command": command,
                      "ok": False, "error": str(exc)}
        self._send(result)
        # The snapshot below already reflects this command's effect, so a
        # coalesced push queued by it would be a duplicate: clear it. A
        # cross-thread change racing in is scheduled on the loop, so its
        # dirty-set runs after this synchronous block and is never lost.
        self.dirty.clear()
        self._send(client_view.snapshot(self._session))

    async def _handle_controller_command(self, msg: dict) -> None:
        command = msg.get("command", "")
        payload = msg.get("payload")
        try:
            if not isinstance(command, str):
                raise ValueError("Command must be a string")
            if payload is None:
                payload = {}
            if not isinstance(payload, dict):
                raise ValueError("Command payload must be an object")
            result = await asyncio.to_thread(self._command_handler, command, payload)
        except (KeyError, TypeError, ValueError, CommandRefused, OSError) as exc:
            _log.error("client command %r failed", command)
            result = {"type": "command_result", "command": command,
                      "ok": False, "error": str(exc)}
        self._send(result)
        self.dirty.clear()
        self._send(await self._snapshot())

    def _send(self, obj: dict) -> None:
        self._writer.write(json.dumps(obj).encode("utf-8") + b"\n")

    async def _drain(self) -> None:
        try:
            await self._writer.drain()
        except ConnectionError:
            pass                                 # reader loop will notice


__all__ = ["ClientServer", "PROTOCOL_VERSION"]
