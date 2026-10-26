"""Bounded setup-only WebSocket rendezvous; never a game transport.

The service authenticates public signed registration records and forwards
one encrypted offer and answer per pending guest. It receives no invitation
capability, owns no poker Session, and forgets a room when its host leaves.
All state belongs to the asyncio loop that calls start().
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from http import HTTPStatus
import ipaddress
import logging
import math
import re
import secrets
import time

from .ice_setup import (canonical, load_json, setup_context,
                        validate_setup_envelope, verify_guest_record,
                        verify_host_record)


class _Refused(ValueError):
    pass


@dataclass(eq=False)
class _Peer:
    socket: object
    ip: str
    challenge: str
    challenge_expiry: int
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    queued_messages: int = 0
    queued_bytes: int = 0
    role: str | None = None
    route: tuple | None = None
    lookup_route: tuple | None = None
    guest_public: str | None = None
    challenge_active: bool = True
    closed: bool = False
    sender: asyncio.Task | None = None


@dataclass
class _Guest:
    peer: _Peer
    record: dict
    size: int
    offer: bool = False
    answer: bool = False


@dataclass
class _Room:
    peer: _Peer
    record: dict
    size: int
    lease: float
    guests: dict[str, _Guest] = field(default_factory=dict)
    attempts: set[tuple[str, str]] = field(default_factory=set)


class IntroductionService:
    """Owned setup server, suitable for ``async with IntroductionService(...)``.

    ``start(host, port)`` returns the bound websockets Server. ``endpoint``
    contains its /v1 URL. ``close()`` / ``aclose()`` await owned connections
    and tasks. Tests may inject a callable clock and smaller limits. TLS
    terminates at a reviewed reverse proxy, or in an explicit ssl context.
    """

    _DEFAULTS = {
        "max_rooms": 128, "max_sockets": 512, "max_challenges": 128,
        "max_guests": 8, "max_retained_bytes": 64 * 1024 * 1024,
        "max_message_bytes": 256 * 1024, "max_queued_messages": 16,
        "max_queued_bytes": 1024 * 1024, "max_rate_buckets": 4096,
        "max_attempts_per_room": 256,
    }

    def __init__(self, service: str, *, limits: dict | None = None, clock=None,
                 trusted_proxies=(), proxy_header="x-forwarded-for", maintenance_interval: float = .25):
        if type(service) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", service) is None:
            raise ValueError("invalid service identifier")
        self.service = service
        self.limits = dict(self._DEFAULTS)
        for name, value in (limits or {}).items():
            if name not in self.limits or type(value) is not int or not 1 <= value <= self._DEFAULTS[name]:
                raise ValueError("limits may only lower positive service bounds")
            self.limits[name] = value
        if (type(maintenance_interval) not in (int, float) or
                not math.isfinite(maintenance_interval) or not 0 < maintenance_interval <= 1):
            raise ValueError("invalid maintenance interval")
        self._interval = maintenance_interval
        self._clock = time.time if clock is None else (clock if callable(clock) else clock.time)
        if not callable(self._clock):
            raise TypeError("clock must provide time")
        self._trusted_proxies = frozenset(str(ipaddress.ip_address(ip)) for ip in trusted_proxies)
        if type(proxy_header) is not str or proxy_header.lower() not in ("x-forwarded-for", "cf-connecting-ip"):
            raise ValueError("invalid trusted proxy header")
        self._proxy_header = proxy_header.lower()
        self._server = None
        self._loop = None
        self._endpoint = None
        self._maintenance_task = None
        self._connections = set()
        self._peers = {}
        self._rooms = {}
        self._challenges = 0
        self._retained_bytes = 0
        self._rates = {}
        self._closing = False
        self._close_task = None
        self._background = set()
        # WebSocket internals may otherwise log exception representations.
        self._logger = logging.Logger("poker.introduction.quiet")
        self._logger.addHandler(logging.NullHandler())
        self._logger.propagate = False

    @property
    def endpoint(self) -> str:
        if self._endpoint is None:
            raise RuntimeError("service is not listening")
        return self._endpoint

    def counts(self) -> dict:
        return {"rooms": len(self._rooms), "sockets": len(self._connections),
                "pending_guests": sum(len(room.guests) for room in self._rooms.values()),
                "pending_challenges": self._challenges,
                "retained_bytes": self._retained_bytes, "rate_buckets": len(self._rates)}

    diagnostics = counts

    async def start(self, host="127.0.0.1", port=0, *, ssl=None):
        if self._server is not None or self._closing:
            raise RuntimeError("service cannot be started again")
        from websockets.asyncio.server import ServerConnection, serve
        self._loop = asyncio.get_running_loop()
        owner = self

        class BoundedConnection(ServerConnection):
            def connection_made(self, transport):
                self.quota_refused = (owner._closing or
                                      len(owner._connections) >= owner.limits["max_sockets"])
                if not self.quota_refused:
                    owner._connections.add(self)
                super().connection_made(transport)
                if self.quota_refused:
                    transport.close()

            def connection_lost(self, exc):
                owner._connections.discard(self)
                super().connection_lost(exc)

        self._server = await serve(
            self._handle, host, port, ssl=ssl, compression=None,
            origins=[None], process_request=self._request,
            open_timeout=5, close_timeout=2, ping_interval=20, ping_timeout=10,
            max_size=self.limits["max_message_bytes"], max_queue=4,
            write_limit=32768, logger=self._logger, server_header=None,
            create_connection=BoundedConnection, backlog=128)
        address = self._server.sockets[0].getsockname()
        endpoint_host = f"[{address[0]}]" if ":" in address[0] else address[0]
        self._endpoint = f"{'wss' if ssl is not None else 'ws'}://{endpoint_host}:{address[1]}/v1"
        self._maintenance_task = asyncio.create_task(self._maintain(), name="poker-introduction-expiry")
        return self._server

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.close()

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine, name="poker-introduction-close-peer")
        self._background.add(task)
        def finished(done):
            self._background.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)

    def _now(self):
        value = self._clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise RuntimeError("invalid service clock")
        return value

    def _client_ip(self, connection):
        address = connection.remote_address
        ip = str(ipaddress.ip_address(address[0])) if address else "unknown"
        if ip in self._trusted_proxies:
            # Honor only the explicitly selected header on a connection from
            # an exact trusted proxy IP. Lists, duplicates and missing values
            # fail closed, without falling back to caller-controlled headers.
            values = connection.request.headers.get_all(self._proxy_header)
            if len(values) != 1:
                raise _Refused()
            ip = str(ipaddress.ip_address(values[0]))
        return ip

    def _allow(self, category, key, capacity, refill):
        now = self._now()
        bucket_key = (category, key)
        previous = self._rates.get(bucket_key)
        if previous is None:
            if len(self._rates) >= self.limits["max_rate_buckets"]:
                raise _Refused()
            tokens = float(capacity)
        else:
            tokens = min(capacity, previous[0] + max(0, now - previous[1]) * refill)
        if tokens < 1:
            raise _Refused()
        self._rates[bucket_key] = (tokens - 1, now)

    async def _request(self, connection, request):
        if connection.quota_refused:
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "Unavailable\n")
        if request.path == "/healthz":
            result = connection.respond(HTTPStatus.OK, canonical(self.counts()).decode() + "\n")
            result.headers["Content-Type"] = "application/json"
            return result
        if request.path != "/v1":
            return connection.respond(HTTPStatus.NOT_FOUND, "Not found\n")
        try:
            self._allow("connect", self._client_ip(connection), 30, .5)
        except (ValueError, TypeError):
            return connection.respond(HTTPStatus.TOO_MANY_REQUESTS, "Unavailable\n")
        return None

    def _reserve(self, size):
        if self._retained_bytes + size > self.limits["max_retained_bytes"]:
            raise _Refused()
        self._retained_bytes += size

    def _queue(self, peer, message):
        raw = canonical(message)
        if (peer.closed or len(raw) > self.limits["max_message_bytes"] or
                peer.queued_messages >= self.limits["max_queued_messages"] or
                peer.queued_bytes + len(raw) > self.limits["max_queued_bytes"]):
            raise _Refused()
        self._reserve(len(raw))
        peer.queued_messages += 1
        peer.queued_bytes += len(raw)
        peer.queue.put_nowait(raw)

    async def _send(self, peer):
        try:
            while True:
                raw = await peer.queue.get()
                try:
                    await asyncio.wait_for(peer.socket.send(raw.decode("utf-8")), timeout=5)
                finally:
                    peer.queued_messages -= 1
                    peer.queued_bytes -= len(raw)
                    self._retained_bytes -= len(raw)
        except asyncio.CancelledError:
            pass
        except Exception:
            await peer.socket.close(code=1013, reason="Setup unavailable")

    async def _handle(self, connection):
        peer = None
        try:
            if self._challenges >= self.limits["max_challenges"]:
                raise _Refused()
            peer = _Peer(connection, self._client_ip(connection), secrets.token_hex(32), int(self._now()) + 30)
            self._peers[connection] = peer
            self._challenges += 1
            peer.sender = asyncio.create_task(self._send(peer), name="poker-introduction-send")
            self._queue(peer, {"type": "challenge", "service": self.service,
                               "challenge": peer.challenge, "expires_at": peer.challenge_expiry})
            while not peer.closed:
                timeout = max(.01, peer.challenge_expiry - self._now()) if peer.role is None else None
                raw = await asyncio.wait_for(connection.recv(), timeout=timeout)
                if type(raw) is str:
                    raw = raw.encode("utf-8")
                message = load_json(raw)
                self._allow("ip-message", peer.ip, 120, 4)
                await self._message(peer, message)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Errors are intentionally indistinguishable and contain neither
            # signed records nor raw exception text.
            await connection.close(code=1008, reason="Setup request refused")
        finally:
            if peer is not None:
                await self._detach(peer)

    @staticmethod
    def _fields(value, names):
        if type(value) is not dict or set(value) != set(names):
            raise _Refused()

    @staticmethod
    def _route(record):
        return tuple(record[key] for key in ("host_public", "discovery_token", "generation"))

    def _challenge(self, peer, record):
        if (not peer.challenge_active or record["challenge"] != peer.challenge or
                self._now() >= peer.challenge_expiry):
            raise _Refused()

    def _consume(self, peer):
        peer.challenge_active = False
        self._challenges -= 1

    def _room(self, route):
        room = self._rooms.get(route)
        if room is None or self._now() >= min(room.lease, room.record["expires_at"]):
            raise _Refused()
        return room

    async def _message(self, peer, message):
        kind = message.get("type")
        if peer.role is None:
            if kind == "lookup":
                self._fields(message, ("type", "host_public", "discovery_token", "generation"))
                for key, size in (("host_public", 64), ("discovery_token", 16), ("generation", 32)):
                    if type(message[key]) is not str or re.fullmatch(r"[0-9a-f]{" + str(size) + "}", message[key]) is None:
                        raise _Refused()
                route = self._route(message)
                room = self._room(route)
                self._allow("lookup", peer.ip, 30, 1)
                peer.lookup_route = route
                self._queue(peer, {"type": "room", "record": room.record})
                return
            self._fields(message, ("type", "record"))
            if kind == "host_register":
                record = verify_host_record(message["record"], service=self.service, now=int(self._now()))
                self._challenge(peer, record)
                self._allow("host", record["host_public"], 4, 1 / 60)
                route = self._route(record)
                if route in self._rooms or len(self._rooms) >= self.limits["max_rooms"]:
                    raise _Refused()
                size = len(canonical(record))
                self._reserve(size)
                room = _Room(peer, record, size, min(self._now() + 60, record["expires_at"]))
                self._rooms[route] = room
                peer.role, peer.route = "host", route
                self._consume(peer)
                self._queue(peer, {"type": "registered", "record": record})
                return
            if kind == "guest_join":
                record = verify_guest_record(message["record"], service=self.service, now=int(self._now()))
                self._challenge(peer, record)
                route = self._route(record)
                room = self._room(route)
                self._allow("room-join", route, 16, 1)
                self._allow("guest", record["guest_public"], 4, 1 / 15)
                attempt = (record["guest_public"], record["guest_nonce"])
                if (peer.lookup_route != route or record["guest_public"] == record["host_public"] or
                        record["expires_at"] > room.record["expires_at"] or
                        record["guest_public"] in room.guests or attempt in room.attempts or
                        len(room.attempts) >= self.limits["max_attempts_per_room"] or
                        len(room.guests) >= self.limits["max_guests"]):
                    raise _Refused()
                size = len(canonical(record))
                self._reserve(size)
                room.guests[record["guest_public"]] = _Guest(peer, record, size)
                room.attempts.add(attempt)
                peer.role, peer.route, peer.guest_public = "guest", route, record["guest_public"]
                self._consume(peer)
                self._queue(peer, {"type": "joined"})
                self._queue(room.peer, {"type": "guest", "record": record})
                return
            raise _Refused()

        room = self._room(peer.route)
        self._allow("room-message", peer.route, 80, 10)
        if kind == "renew" and peer.role == "host":
            self._fields(message, ("type",))
            room.lease = min(self._now() + 60, room.record["expires_at"])
            self._queue(peer, {"type": "renewed"})
            return
        if kind == "close" and peer.role == "host":
            self._fields(message, ("type",))
            self._remove_room(peer.route)
            await peer.socket.close(code=1000, reason="Setup room closed")
            return
        if kind == "reject" and peer.role == "host":
            self._fields(message, ("type", "guest_public"))
            if type(message["guest_public"]) is not str or re.fullmatch(r"[0-9a-f]{64}", message["guest_public"]) is None:
                raise _Refused()
            guest = room.guests.get(message["guest_public"])
            if guest is None:
                # A guest can leave or expire while the host's ICE attempt is
                # still timing out. Its late cancellation must not close the
                # host room or interrupt unrelated pending joins.
                return
            self._remove_guest(room, guest)
            self._spawn(guest.peer.socket.close(code=1008, reason="Setup request refused"))
            return
        self._fields(message, ("type", "guest_public", "envelope"))
        if kind != "setup" or type(message["guest_public"]) is not str:
            raise _Refused()
        guest = room.guests.get(message["guest_public"])
        if guest is None or (peer.role == "guest" and guest.peer is not peer):
            raise _Refused()
        self._allow("guest-message", (peer.route, message["guest_public"]), 12, 2)
        context = setup_context(room.record, guest.record, now=int(self._now()))
        offer = peer.role == "host"
        if (guest.offer if offer else guest.answer or not guest.offer):
            raise _Refused()
        validate_setup_envelope(
            message["envelope"], context, kind="offer" if offer else "answer",
            signer=room.record["host_public"] if offer else guest.record["guest_public"],
            recipient=guest.record["setup_public"] if offer else room.record["setup_public"],
            now=int(self._now()))
        self._queue(guest.peer if offer else room.peer, message)
        if offer:
            guest.offer = True
        else:
            guest.answer = True

    def _remove_guest(self, room, guest):
        if room.guests.get(guest.record["guest_public"]) is guest:
            del room.guests[guest.record["guest_public"]]
            self._retained_bytes -= guest.size

    def _remove_room(self, route):
        room = self._rooms.pop(route, None)
        if room is None:
            return
        self._retained_bytes -= room.size
        for guest in list(room.guests.values()):
            self._remove_guest(room, guest)
            self._spawn(guest.peer.socket.close(code=1000, reason="Setup room closed"))

    async def _detach(self, peer):
        peer.closed = True
        if peer.challenge_active:
            self._consume(peer)
        if peer.role == "host":
            self._remove_room(peer.route)
        elif peer.role == "guest":
            room = self._rooms.get(peer.route)
            guest = room.guests.get(peer.guest_public) if room is not None else None
            if guest is not None and guest.peer is peer:
                self._remove_guest(room, guest)
        if peer.sender is not None:
            peer.sender.cancel()
            await asyncio.gather(peer.sender, return_exceptions=True)
        while not peer.queue.empty():
            raw = peer.queue.get_nowait()
            peer.queued_messages -= 1
            peer.queued_bytes -= len(raw)
            self._retained_bytes -= len(raw)
        self._peers.pop(peer.socket, None)

    async def _maintain(self):
        try:
            while True:
                await asyncio.sleep(self._interval)
                now = self._now()
                self._rates = {key: value for key, value in self._rates.items() if now - value[1] <= 300}
                for peer in list(self._peers.values()):
                    if peer.challenge_active and not peer.closed and now >= peer.challenge_expiry:
                        peer.closed = True
                        self._spawn(peer.socket.close(code=1008, reason="Setup challenge expired"))
                for route, room in list(self._rooms.items()):
                    if now >= min(room.lease, room.record["expires_at"]):
                        self._remove_room(route)
                        self._spawn(room.peer.socket.close(code=1000, reason="Setup room expired"))
                    else:
                        for guest in list(room.guests.values()):
                            if now >= guest.record["expires_at"]:
                                self._remove_guest(room, guest)
                                self._spawn(guest.peer.socket.close(code=1000, reason="Setup attempt expired"))
        except asyncio.CancelledError:
            pass

    async def close(self):
        if self._loop is not None and asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("service must close on its creating loop")
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._close(), name="poker-introduction-close")
        await asyncio.shield(self._close_task)

    aclose = close

    async def _close(self):
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            await asyncio.gather(self._maintenance_task, return_exceptions=True)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._background:
            await asyncio.gather(*tuple(self._background), return_exceptions=True)
        self._rates.clear()
