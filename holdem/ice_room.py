"""Casual poker over authenticated direct peers, independent of presentation.

The containing host supplies identity, master secret and the invite capability.
It authenticates setup, attaches established peers, awaits commands/views and
owns aclose(). Existing Session, admission, rules and cryptography stay in use.
"""
from __future__ import annotations

import asyncio
import math
import threading

from . import native_crypto
from .embedding import SessionPlayer
from .ice_transport import DirectSessionTransport
from .peer_rules import table_settings
from .p2p.admission import HostAdmission, JoinerAdmission
from .p2p.join_auth import JoinAuthenticator
from .p2p.session import Session
from .p2p.timeout import RealClock


class DirectPokerRoom:
    def __init__(self, *, is_host: bool, identity, master_secret: bytes,
                 nickname: str, admission_secret: bytes, discovery_token: bytes,
                 host_public_key: bytes, settings: dict | None = None,
                 on_change=None, clock=None, admission_timeout: float = 15.0) -> None:
        self._loop = asyncio.get_running_loop()
        if type(is_host) is not bool:
            raise TypeError("is_host must be a bool")
        if type(nickname) is not str or not nickname.strip() or len(nickname) > 40:
            raise ValueError("player name must contain 1–40 characters")
        if type(master_secret) is not bytes or len(master_secret) != 32:
            raise ValueError("host must supply a 32-byte master secret")
        if type(host_public_key) is not bytes or len(host_public_key) != 32:
            raise ValueError("a full host public key is required")
        if is_host and host_public_key != identity.public_key_bytes():
            raise ValueError("host identity differs from the room pin")
        if (type(admission_timeout) not in (int, float) or not math.isfinite(admission_timeout) or
                not 0 < admission_timeout <= 30):
            raise ValueError("admission timeout must be finite and between zero and 30 seconds")
        for name in ("peer_id", "public_key_bytes", "x25519_public_key_bytes", "sign"):
            if not callable(getattr(identity, name, None)):
                raise TypeError("host must supply a complete peer identity")
        if len(identity.x25519_public_key_bytes()) != 32:
            raise ValueError("a full companion X25519 public key is required")
        self.settings = table_settings(settings or {})
        clock = clock or RealClock()
        policy = HostAdmission(admission_secret, host_public_key, discovery_token) if is_host else None
        pin = None if is_host else JoinerAdmission(admission_secret, host_public_key,
                                                   identity.public_key_bytes(), discovery_token)
        native_crypto.initialize()
        self._metadata = threading.RLock()
        self._closed = False
        self._terminal = False
        self._close_task = None
        self._pending_calls = set()
        self._admission_tasks = {}
        self._admitted_connections = set()
        self._admitted = False
        self._admission_timeout = admission_timeout
        self._on_change = on_change
        self._identity = identity
        self._host_key = host_public_key
        self._host_connection = None
        self._error = None
        self.transport = DirectSessionTransport(signer=identity, clock=clock, allow_forwarding=is_host)
        self.session = Session(is_host=is_host, nickname=nickname, avatar_b64="",
                               transport=self.transport, clock=clock, master_secret=master_secret,
                               admission=policy, joiner_admission=pin)
        self.session.enable_casual_reliability()
        self.session.on_state_changed = self._notify
        self.session.on_player_list_changed = self._roster_changed
        self.session.on_game_start = self._game_started
        self.session.on_session_terminated = self._terminated
        self.transport.on_disconnect(self._disconnected, with_reason=True)
        self.transport.on_task_error(self._failed)
        self.player = SessionPlayer(self.session, start_table=self._start)
        self._authenticator = None
        if is_host:
            self.transport.on_message(self._host_message)
            self.session.local_conn_id = identity.peer_id()
            self.session.add_local_player(identity.peer_id(), public_key=identity.public_key_bytes(),
                                          x25519_public_key=identity.x25519_public_key_bytes())
        else:
            self._authenticator = JoinAuthenticator(
                transport=self.transport, session=self.session, joiner_admission=pin,
                nickname=nickname, signer=identity, clock=clock,
                on_failed=lambda _reason: self._failed(None))
            self.transport.on_message(self._guest_message)
        self._ticker = self._loop.create_task(self._tick(), name="poker-direct-room-clock")

    def _notify(self):
        with self._metadata:
            callback = self._on_change if not self._closed else None
        if callback is not None:
            try:
                callback()
            except Exception:
                pass

    def _terminated(self, _record):
        with self._metadata:
            self._terminal = True
        self._notify()

    def _roster_changed(self, players):
        present = {p.conn_id for p in players}
        with self._metadata:
            self._admitted_connections = present
            self._admitted = self.session.local_conn_id in present
            tasks = [task for cid, task in self._admission_tasks.items()
                     if cid in present or (not self.session.is_host and self._admitted)]
        for task in tasks:
            self._loop.call_soon_threadsafe(task.cancel)
        self._notify()

    def _failed(self, _error):
        with self._metadata:
            self._error = "The table could not authenticate or process this connection"
        self.session.terminate(Session.ABORTED_PROTOCOL, self._error)

    def _host_message(self, cid, message):
        with self._metadata:
            if self._closed:
                return
        with self.session.read_state():
            if self.session.state != "LOBBY" and not any(p.conn_id == cid for p in self.session.player_list()):
                self.transport.disconnect(cid)
                return
            self.session.handle_message(cid, message)

    def _guest_message(self, cid, message):
        with self._metadata:
            if self._closed:
                return
        self._authenticator.route(cid, message)

    def _disconnected(self, cid, reason):
        with self._metadata:
            if self._closed:
                return
            host_connection = self._host_connection
            admitted = self._admitted
            timer = self._admission_tasks.get(cid)
        if timer is not None:
            self._loop.call_soon_threadsafe(timer.cancel)
        if not self.session.is_host and cid == host_connection:
            description = ("Clocks differ too much. Check automatic date and time and join again."
                           if reason == "clock" else "The host disconnected. Leave and join a new room.")
            if not admitted and reason != "clock":
                description = "The room could not authenticate. Check the invite and join again."
            self.session.terminate(Session.HOST_LOST if admitted else Session.ABORTED_PROTOCOL, description)
        self.session.handle_disconnect(cid, reason=reason)
        self._notify()

    def _game_started(self, payload):
        try:
            settings = table_settings(payload.get("table_settings", {}))
            if not self.session.reliability_lobby_status()["ready"]:
                raise ValueError("roster clocks are not ready")
            order = self.session.seat_order
            if not 2 <= len(order) <= 9:
                raise ValueError("a peer game requires two to nine players")
            names = {p.conn_id: p.nickname for p in self.session.player_list()}
            self.session.reliability_started()
            self.session.start_p2p_hand(hand_no=1, names=[names[cid] for cid in order],
                                       stacks=[settings["stack"]] * len(order), sb=settings["sb"],
                                       bb=settings["bb"], structure=settings["structure"], button=0)
        except Exception:
            self.session.terminate(Session.ABORTED_PROTOCOL, "The table could not start its first hand")
        self._notify()

    def _start(self):
        if not self.session.is_host:
            raise ValueError("only the host can start")
        if not self.session.reliability_lobby_status()["ready"]:
            return "not_ready"
        return self.session.start_ready_game(dict(self.settings))

    async def _call(self, operation, *, closing=False):
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("room must stay on its creating asyncio loop")
        with self._metadata:
            if self._closed and not closing:
                raise RuntimeError("room is closed")
        future = self._loop.create_future()
        self._pending_calls.add(future)
        def complete(result, error):
            if not future.done():
                if error is not None:
                    future.set_exception(error)
                else:
                    future.set_result(result)
        def perform():
            try:
                with self._metadata:
                    if self._closed and not closing:
                        raise RuntimeError("room is closed")
                result = operation()
            except Exception as error:
                self._loop.call_soon_threadsafe(complete, None, error)
            else:
                self._loop.call_soon_threadsafe(complete, result, None)
        if not self.transport.dispatch_work(perform):
            self._pending_calls.discard(future)
            raise RuntimeError("room command queue refused work")
        try:
            return await future
        finally:
            self._pending_calls.discard(future)

    async def attach_peer(self, peer, *, remote_key: bytes, conn_id=None):
        def allowed():
            if self.session.state != "LOBBY" or self.session.terminal_state is not None:
                raise RuntimeError("this room no longer accepts connections")
        await self._call(allowed)
        if not self.session.is_host:
            with self._metadata:
                if self._host_connection is not None:
                    raise RuntimeError("a joiner can attach only one host")
            if remote_key != self._host_key:
                raise ValueError("setup peer differs from the full host pin")
        cid = await self.transport.attach_peer(peer, remote_key=remote_key, conn_id=conn_id,
                                              remote_can_forward=not self.session.is_host)
        if not self.session.is_host:
            with self._metadata:
                self._host_connection = cid
            await self._call(lambda: self._authenticator.begin(cid))
        self._start_admission_timer(cid)
        return cid

    def _start_admission_timer(self, cid):
        with self._metadata:
            if (self._closed or cid in self._admitted_connections or
                    (not self.session.is_host and self._admitted)):
                return
            task = self._loop.create_task(self._expire_admission(cid), name="poker-direct-admission")
            self._admission_tasks[cid] = task
        def finished(done):
            with self._metadata:
                if self._admission_tasks.get(cid) is done:
                    self._admission_tasks.pop(cid, None)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)

    async def _expire_admission(self, cid):
        try:
            await asyncio.sleep(self._admission_timeout)
            def expire():
                with self.session.read_state():
                    admitted = (any(p.conn_id == cid for p in self.session.player_list())
                                if self.session.is_host else self._admitted)
                    if admitted or self.session.terminal_state is not None:
                        return
                    if not self.session.is_host:
                        self._error = "The room did not finish authenticating. Check the invite and join again."
                        self.session.terminate(Session.ABORTED_PROTOCOL, self._error)
                    self.transport.disconnect(cid)
            try:
                await self._call(expire)
            except RuntimeError:
                # A saturated callback queue must not retain an unauthenticated
                # channel indefinitely. Disconnect is a socket-loop operation.
                with self._metadata:
                    if not self._closed and cid not in self._admitted_connections:
                        self.transport.disconnect(cid, reason="protocol")
        except asyncio.CancelledError:
            pass

    async def set_ready(self, ready: bool):
        if type(ready) is not bool:
            raise ValueError("ready must be a bool")
        def change():
            with self.session.read_state():
                if self.session.state != "LOBBY" or self.session.terminal_state is not None:
                    raise RuntimeError("ready can only change in a live lobby")
                if not any(p.conn_id == self.session.local_conn_id for p in self.session.player_list()):
                    raise RuntimeError("wait for admission")
                if self.session.is_host:
                    self.session.set_ready(self.session.local_conn_id, ready)
                else:
                    self.transport.send(self._host_connection, {"type": "ready", "payload": {"ready": ready}})
        return await self._call(change)

    async def command(self, command, payload=None):
        return await self._call(lambda: self.player.command(command, payload))

    async def view(self):
        return await self._call(self.player.view)

    async def lobby(self):
        def read():
            with self.session.read_state():
                players = self.session.player_list()
                status = self.session.reliability_lobby_status()
                return {"players": [{"conn_id": p.conn_id, "nickname": p.nickname, "ready": p.ready,
                                     "seat": index, "is_you": p.conn_id == self.session.local_conn_id,
                                     "is_host": bool(p.is_host)}
                                    for index, p in enumerate(players)],
                        "can_start": (self.session.is_host and self.session.state == "LOBBY" and
                                      self.session.terminal_state is None and 2 <= len(players) <= 9 and
                                      self.session.all_ready and status["ready"]),
                        "clock_ready": status["ready"], "terminal_state": self.session.terminal_state,
                        "error": self._error, "host": self.session.is_host}
        return await self._call(read)

    async def _tick(self):
        try:
            while True:
                await asyncio.sleep(.25)
                with self._metadata:
                    if self._closed or self._terminal:
                        return
                # Ticks use actual clock time, so a full bounded queue may
                # skip this submission and retry; it must not kill the timer.
                self.transport.dispatch_work(self.session.reliability_tick)
        except asyncio.CancelledError:
            pass

    async def aclose(self):
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("room must stay on its creating asyncio loop")
        with self._metadata:
            self._closed = True
        if self._close_task is None:
            self._close_task = self._loop.create_task(self._close(), name="poker-direct-room-close")
        await asyncio.shield(self._close_task)

    async def _close(self):
        self._ticker.cancel()
        with self._metadata:
            timers = list(self._admission_tasks.values())
        for task in timers:
            task.cancel()
        await asyncio.gather(self._ticker, *timers, return_exceptions=True)
        for future in list(self._pending_calls):
            future.cancel()
        try:
            await self._call(lambda: self.session.terminate(Session.LOCAL_SHUTDOWN, "You left the room"), closing=True)
        finally:
            await self.transport.aclose()
