"""Desktop-owned Internet setup, with direct peer-verified poker after joining.

InternetSession lives on a caller-owned asyncio loop. InternetTable is the
synchronous facade for the desktop's off-loop command/snapshot callbacks.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hmac
import os
import secrets
import threading
import time

from .ice_peer import DirectPeer
from .ice_room import DirectPokerRoom
from .introduction_client import IntroductionConnection, validate_endpoint


def configured_services():
    # A containing game may supply its own trusted map. No URL is taken from
    # an invite. A production default is added only after deployment review.
    selected = os.environ.get("HOLDEM_INTRODUCTION_URL", "")
    return {"poker": validate_endpoint(selected)} if selected else {}


def _empty_snapshot():
    from . import player_info
    return dict(type="snapshot", phase="lobby", hand_num=0, seats=[], you={"seat": -1},
                deal_policy=None, session_over=False, terminal=None, events=[], settlement=None,
                turn=dict(state="lobby", headline="Connecting", street="idle", actor=-1,
                          pot=0, your_stack=0, effective_stack=0),
                deal_progress=player_info.deal_progress_view("lobby"))


class InternetSession:
    def __init__(self, *, nickname, settings, on_change=None, services=None,
                 identity=None, master_secret=None, stun_urls=None):
        self._loop = asyncio.get_running_loop()
        self.nickname, self.settings = nickname, dict(settings)
        self.services = dict(configured_services() if services is None else services)
        self._allow_loopback = services is not None
        for endpoint in self.services.values():
            validate_endpoint(endpoint, allow_loopback=self._allow_loopback)
        self._identity, self._master = identity, master_secret
        self._stun_urls = stun_urls
        self._on_change = on_change
        self._room = None
        self._intro = None
        self._main_task = None
        self._renew_task = None
        self._pending = {}
        self._seen = set()
        self._attach_lock = asyncio.Lock()
        self._mode, self._state = "menu", "idle"
        self._error = self._status = self._invite = ""
        self._closed = False
        self._accepting = False
        self._starting = False
        self._epoch = 0
        self._close_task = None
        self._cleanup_failed = False
        self._failed_peers = []

    def _notify(self):
        if self._on_change is not None and not self._closed:
            try:
                self._on_change()
            except Exception:
                pass

    def _active(self, epoch):
        if self._closed or epoch != self._epoch:
            raise asyncio.CancelledError()

    def _cleanup_failure(self):
        # A failed owned close remains observable even after a later retry.
        # Never retain or display exception strings from setup/native code.
        self._cleanup_failed = True
        self._error = "Internet connection cleanup did not complete. Close the game and try again."
        self._notify()

    async def _cleanup_resource(self, resource, *, peer=False):
        try:
            await (resource.close() if peer else resource.aclose())
        except (Exception, asyncio.CancelledError):
            self._cleanup_failure()
            if peer and not any(owned is resource for owned in self._failed_peers):
                self._failed_peers.append(resource)
            return False
        return True

    def _observe_setup_task(self, task):
        if not task.cancelled() and task.exception() is not None:
            # Normal denied invites/network failures are handled in the
            # setup body. An escaping failure must not become a green close.
            self._cleanup_failure()

    async def _stop_setup_tasks(self):
        if self._renew_task is not None:
            self._renew_task.cancel()
            # Renewal can fail normally when the service disappears. That
            # closes admission, independently of the established direct game.
            await asyncio.gather(self._renew_task, return_exceptions=True)
        pending = list(self._pending.values())
        for item in pending:
            answer = item.get("answer")
            if answer is not None and not answer.done():
                answer.cancel()
            item["task"].cancel()
        await asyncio.gather(*(item["task"] for item in pending), return_exceptions=True)
        for item in pending:
            self._observe_setup_task(item["task"])
        self._pending.clear()

    async def _prepare(self, *, is_host, pin, secret, token):
        if self._identity is None:
            from .p2p.identity import load_or_create
            self._identity = await asyncio.to_thread(load_or_create)
        if self._master is None:
            from .p2p.device_secret import load_or_create
            self._master = await asyncio.to_thread(load_or_create)
        if self._closed:
            raise asyncio.CancelledError()
        self._room = DirectPokerRoom(is_host=is_host, identity=self._identity,
                                     master_secret=self._master, nickname=self.nickname,
                                     admission_secret=secret, discovery_token=token,
                                     host_public_key=(self._identity.public_key_bytes() if is_host else pin),
                                     settings=self.settings, on_change=self._notify)

    async def host(self):
        if self._mode != "menu" or self._closed:
            raise ValueError("Leave the current table before hosting")
        if not self.services:
            raise ValueError("Internet hosting is not configured in this build")
        self._mode, self._state = "host", "connecting"
        self._status = "Creating an Internet table..."
        self._accepting = True
        self._main_task = self._loop.create_task(self._host(self._epoch), name="poker-internet-host")
        self._notify()

    async def join(self, code):
        from .internet_invite import parse_invite
        invite = parse_invite(code, services=self.services)
        if self._mode != "menu" or self._closed:
            raise ValueError("Leave the current table before joining")
        self._mode, self._state = "join", "connecting"
        self._status = "Finding your friend's table..."
        self._main_task = self._loop.create_task(self._join(invite, self._epoch), name="poker-internet-join")
        self._notify()

    async def _connection(self, service):
        connection = await IntroductionConnection.connect(self.services[service], service,
                                                          allow_loopback=self._allow_loopback)
        self._intro = connection
        if self._closed:
            await self._cleanup_resource(connection)
            raise asyncio.CancelledError()
        return connection

    async def _host(self, epoch):
        from .ice_setup import sign_record, verify_guest_record, capability_proof, setup_context
        from .internet_invite import InternetInvite, encode_invite
        connection = None
        try:
            service = next(iter(self.services))
            secret, token, generation = secrets.token_bytes(16), secrets.token_bytes(8), secrets.token_hex(16)
            await self._prepare(is_host=True, pin=None, secret=secret, token=token)
            self._active(epoch)
            connection = await self._connection(service)
            now = int(time.time())
            record = sign_record({"v": 1, "purpose": "poker-direct-room-v1", "service": service,
                                  "host_public": self._identity.public_key_bytes().hex(),
                                  "discovery_token": token.hex(), "generation": generation,
                                  "setup_public": self._identity.x25519_public_key_bytes().hex(),
                                  "challenge": connection.challenge, "created_at": now, "expires_at": now + 1800},
                                 self._identity)
            await connection.send({"type": "host_register", "record": record})
            reply = await connection.receive(10)
            if reply != {"type": "registered", "record": record}:
                raise ValueError("The introduction service did not register this room")
            self._active(epoch)
            self._invite = encode_invite(InternetInvite(service=service, host_public=record["host_public"],
                                                       discovery_token=token.hex(), generation=generation,
                                                       expires_at=record["expires_at"], admission_secret=secret.hex()))
            self._state, self._status = "lobby", "Share this invite with friends. Everyone must be ready."
            self._renew_task = self._loop.create_task(self._renew(connection), name="poker-internet-renew")
            self._notify()
            while self._accepting:
                message = await connection.receive(50)
                self._active(epoch)
                if message.get("type") in ("renewed", "rejected"):
                    continue
                if message.get("type") == "guest":
                    guest = verify_guest_record(message.get("record"), service=service)
                    context = setup_context(record, guest)
                    public, nonce = guest["guest_public"], guest["guest_nonce"]
                    if (public in self._pending or nonce in self._seen or len(self._seen) >= 256 or
                            len(self._pending) >= 8 or not hmac.compare_digest(
                                guest["capability_proof"], capability_proof(secret, guest, record))):
                        await connection.send({"type": "reject", "guest_public": public})
                        continue
                    self._seen.add(nonce)
                    task = self._loop.create_task(self._connect_guest(connection, guest, context, epoch),
                                                  name="poker-internet-guest-setup")
                    self._pending[public] = {"task": task, "answer": self._loop.create_future()}
                    task.add_done_callback(lambda done, key=public: self._guest_done(key, done))
                elif message.get("type") == "setup":
                    pending = self._pending.get(message.get("guest_public"))
                    if pending is None or pending["answer"].done():
                        raise ValueError("Unexpected or repeated setup answer")
                    pending["answer"].set_result(message.get("envelope"))
                else:
                    raise ValueError("Unexpected introduction message")
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._closed and not self._starting:
                self._accepting = False
                if self._state == "connecting":
                    self._state = "closed"
                    self._error = "Could not create an Internet table. Check your connection and try again."
                else:
                    self._error = "New friends cannot join right now. Connected players can still start or continue."
                self._notify()
        finally:
            if connection is not None:
                await self._cleanup_resource(connection)
            await self._stop_setup_tasks()
            if self._state == "closed" and self._room is not None:
                if await self._cleanup_resource(self._room):
                    self._room = None

    def _guest_done(self, public, task):
        self._pending.pop(public, None)
        self._observe_setup_task(task)

    async def _renew(self, connection):
        while True:
            await asyncio.sleep(20)
            await connection.send({"type": "renew"})

    def _peer(self):
        return DirectPeer(**({"stun_urls": self._stun_urls} if self._stun_urls is not None else {}))

    async def _admitted(self):
        while True:
            lobby = await self._room.lobby()
            if lobby["terminal_state"] is not None:
                raise ValueError("The room did not authenticate")
            if any(p["is_you"] for p in lobby["players"]):
                return
            await asyncio.sleep(.02)

    async def _connect_guest(self, connection, guest, context, epoch):
        from .ice_setup import seal_description, open_description
        peer, attached = self._peer(), False
        try:
            async def connect():
                nonlocal attached
                description = await peer.create_offer()
                self._active(epoch)
                await connection.send({"type": "setup", "guest_public": guest["guest_public"],
                                       "envelope": seal_description(description, context, self._identity,
                                                                    guest["setup_public"])})
                envelope = await self._pending[guest["guest_public"]]["answer"]
                description = open_description(envelope, context, self._identity,
                                               kind="answer", signer=guest["guest_public"])
                await peer.accept_answer(description)
                await peer.wait_open(30)
                async with self._attach_lock:
                    self._active(epoch)
                    if not self._accepting:
                        raise asyncio.CancelledError()
                    await self._room.attach_peer(peer, remote_key=bytes.fromhex(guest["guest_public"]))
                    attached = True
            await asyncio.wait_for(connect(), max(1, min(70, context["expires_at"] - int(time.time()))))
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._closed and self._accepting:
                try:
                    await connection.send({"type": "reject", "guest_public": guest["guest_public"]})
                except Exception:
                    pass
        finally:
            if not attached:
                await self._cleanup_resource(peer, peer=True)

    async def _join(self, invite, epoch):
        from .ice_setup import (verify_host_record, sign_record, capability_proof, setup_context,
                                seal_description, open_description)
        connection, peer, attached = None, None, False
        try:
            await self._prepare(is_host=False, pin=bytes.fromhex(invite.host_public),
                                secret=bytes.fromhex(invite.admission_secret), token=bytes.fromhex(invite.discovery_token))
            self._active(epoch)
            connection = await self._connection(invite.service)
            await connection.send({"type": "lookup", "host_public": invite.host_public,
                                   "discovery_token": invite.discovery_token, "generation": invite.generation})
            reply = await connection.receive(10)
            if set(reply) != {"type", "record"} or reply["type"] != "room":
                raise ValueError("The table could not be found")
            host = verify_host_record(reply["record"], service=invite.service)
            if any(host[k] != getattr(invite, k) for k in
                   ("host_public", "discovery_token", "generation", "expires_at")):
                raise ValueError("The table differs from the invite")
            now = int(time.time())
            guest = {"v": 1, "purpose": "poker-direct-guest-v1", "service": invite.service,
                     "host_public": invite.host_public, "discovery_token": invite.discovery_token,
                     "generation": invite.generation, "guest_public": self._identity.public_key_bytes().hex(),
                     "setup_public": self._identity.x25519_public_key_bytes().hex(),
                     "guest_nonce": secrets.token_hex(32), "challenge": connection.challenge,
                     "created_at": now, "expires_at": min(now + 90, invite.expires_at)}
            guest["capability_proof"] = capability_proof(bytes.fromhex(invite.admission_secret), guest, host)
            guest = sign_record(guest, self._identity)
            context = setup_context(host, guest)
            await connection.send({"type": "guest_join", "record": guest})
            if await connection.receive(10) != {"type": "joined"}:
                raise ValueError("The host could not accept this join")
            self._status = "Connecting directly to your friend's table..."
            self._notify()
            offer = await connection.receive(40)
            if (set(offer) != {"type", "guest_public", "envelope"} or offer["type"] != "setup" or
                    offer["guest_public"] != guest["guest_public"]):
                raise ValueError("Unexpected direct setup")
            description = open_description(offer["envelope"], context, self._identity,
                                           kind="offer", signer=invite.host_public)
            peer = self._peer()
            answer = await peer.accept_offer(description)
            self._active(epoch)
            await connection.send({"type": "setup", "guest_public": guest["guest_public"],
                                   "envelope": seal_description(answer, context, self._identity, host["setup_public"])})
            await peer.wait_open(30)
            self._active(epoch)
            await self._room.attach_peer(peer, remote_key=bytes.fromhex(invite.host_public))
            attached = True
            await asyncio.wait_for(self._admitted(), 16)
            self._active(epoch)
            self._state, self._status = "lobby", "Connected. Press Ready when you want the host to start."
            self._notify()
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._closed:
                self._state = "closed"
                self._error = ("Could not join this table. Check the invite and connection, then try again. "
                               "Some networks cannot make a direct connection.")
                self._notify()
        finally:
            if connection is not None:
                await self._cleanup_resource(connection)
            if peer is not None and not attached:
                await self._cleanup_resource(peer, peer=True)
            if self._state == "closed" and self._room is not None:
                if await self._cleanup_resource(self._room):
                    self._room = None

    async def set_ready(self, ready):
        if self._state != "lobby" or self._room is None:
            raise ValueError("Wait for the table to connect")
        await self._room.set_ready(ready)

    async def start(self):
        if self._room is None or self._mode != "host":
            raise ValueError("Only the host can start this table")
        async with self._attach_lock:
            if not (await self._room.lobby())["can_start"]:
                return "not_ready"
            was_accepting = self._accepting
            self._accepting, self._starting = False, True
            try:
                verdict = await self._room.command("start_game")
            except BaseException:
                self._accepting, self._starting = was_accepting, False
                raise
            if not verdict["ok"]:
                self._accepting, self._starting = was_accepting, False
                return verdict["verdict"]
            self._state, self._status, self._error, self._invite = "playing", "", "", ""
        # The serialized core has accepted Start while attachments were
        # excluded. A raced Ready change can no longer strand a live lobby.
        if self._main_task is not None:
            self._main_task.cancel()
            await asyncio.gather(self._main_task, return_exceptions=True)
        self._notify()
        return verdict["verdict"]

    async def command(self, command, payload=None):
        if command == "start_game":
            verdict = await self.start()
            return {"type": "command_result", "command": command, "verdict": verdict, "ok": verdict == "started"}
        if self._room is None or self._state not in ("lobby", "playing"):
            raise ValueError("Wait for the table to connect")
        return await self._room.command(command, payload)

    async def snapshot(self):
        room = self._room
        try:
            snap = await room.view() if room is not None else _empty_snapshot()
            lobby = await room.lobby() if room is not None else {"players": [], "can_start": False}
        except RuntimeError:
            if not self._closed and self._state != "closed" and room is self._room:
                raise
            snap, lobby = _empty_snapshot(), {"players": [], "can_start": False}
        state = self._state
        if state not in ("connecting", "closed"):
            if snap.get("terminal") is not None:
                state = "closed"
            elif snap.get("phase") != "lobby":
                state = "playing"
        snap["lobby"] = {"mode": self._mode, "network": "internet", "state": state,
                         "nickname": self.nickname, "invite": self._invite if state == "lobby" else "",
                         "error": self._error, "status": self._status,
                         "can_start": state == "lobby" and lobby["can_start"],
                         "players": [{"seat": p["seat"], "name": p["nickname"], "ready": p["ready"],
                                      "is_you": p["is_you"], "is_host": p["is_host"]} for p in lobby["players"]]}
        return snap

    async def aclose(self):
        if self._close_task is None:
            self._closed, self._accepting = True, False
            self._epoch += 1
            self._close_task = self._loop.create_task(self._close(), name="poker-internet-close")
        await asyncio.shield(self._close_task)

    async def _close(self):
        if self._main_task is not None:
            self._main_task.cancel()
            await asyncio.gather(self._main_task, return_exceptions=True)
            self._observe_setup_task(self._main_task)
        await self._stop_setup_tasks()
        if self._intro is not None:
            await self._cleanup_resource(self._intro)
        for peer in tuple(self._failed_peers):
            if await self._cleanup_resource(peer, peer=True):
                self._failed_peers.remove(peer)
        if self._room is not None:
            await self._cleanup_resource(self._room)
        self._state, self._invite, self._status = "closed", "", ""
        if self._cleanup_failed:
            raise RuntimeError("Internet connection cleanup did not complete") from None


class InternetTable:
    """Synchronous desktop facade; call from a worker, never its asyncio loop."""

    def __init__(self, *, loop, nickname, settings, on_change, services=None):
        if loop is None or not loop.is_running():
            raise ValueError("Internet play requires the running desktop loop")
        try:
            if asyncio.get_running_loop() is loop:
                raise ValueError("Use InternetSession directly on its asyncio loop")
        except RuntimeError:
            pass
        self._loop = loop
        self._owner_thread = None
        self._session = self._call(self._create(nickname, settings, on_change, services))

    async def _create(self, nickname, settings, on_change, services):
        self._owner_thread = threading.get_ident()
        return InternetSession(nickname=nickname, settings=settings, on_change=on_change, services=services)

    def _call(self, coroutine, timeout=25):
        if threading.get_ident() == self._owner_thread:
            coroutine.close()
            raise RuntimeError("Use the asynchronous InternetSession on its own loop")
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise RuntimeError("The Internet operation did not finish") from None

    def host(self):
        return self._call(self._session.host())

    def join(self, code):
        return self._call(self._session.join(code))

    def set_ready(self, ready):
        return self._call(self._session.set_ready(ready))

    def start(self):
        return self._call(self._session.start())

    def command(self, command, payload=None):
        return self._call(self._session.command(command, payload))

    def snapshot(self):
        return self._call(self._session.snapshot())

    def close(self):
        return self._call(self._session.aclose(), timeout=30)

    async def aclose(self):
        await self._session.aclose()
