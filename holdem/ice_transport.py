"""Host-owned signed Session transport over established direct byte peers.

The host authenticates setup before attach_peer and supplies its identity and
clock. Poker admission remains mandatory in Session/JoinAuthenticator. One
transport owns one serialized callback worker and its attached DirectPeers;
the embedding host owns the asyncio loop and always awaits aclose().
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import json
import math
import threading
import uuid

from .ice_peer import DirectPeer
from .p2p import wire
from .p2p.dispatch import MessageWorker


# Nine-player deals can burst roughly 189 small signed envelopes before
# native writes catch up. The independent byte bound still caps large frames.
_MAX_QUEUED_ENVELOPES = 256


class DirectTransportError(ConnectionError):
    """The owned network adapter failed without disclosing private frames."""


@dataclass
class _Link:
    conn_id: str
    peer: DirectPeer
    remote_key: str
    remote_can_forward: bool
    tasks: set = field(default_factory=set)
    queued_bytes: int = 0
    queued_messages: int = 0
    outgoing: deque[bytes] = field(default_factory=deque)
    send_task: asyncio.Task | None = None
    closing: bool = False
    close_task: asyncio.Task | None = None


class DirectSessionTransport:
    """Reliable ordered signed envelopes, with one independent channel per peer.

    Hosts enable allow_forwarding to courier other players' original signatures.
    A joiner enables remote_can_forward only for its authenticated host channel.
    No unsigned control-message exception exists on this transport.
    """

    delivers_verified_envelopes = True

    def __init__(self, *, signer, clock, allow_forwarding: bool = False,
                 heartbeat_interval: float = 5.0, silence_timeout: float = 20.0,
                 queue_bytes: int = 2 * 1024 * 1024) -> None:
        self._loop = asyncio.get_running_loop()
        if type(allow_forwarding) is not bool:
            raise ValueError("allow_forwarding must be a bool")
        if (type(heartbeat_interval) not in (int, float) or
                type(silence_timeout) not in (int, float) or
                not math.isfinite(heartbeat_interval) or not math.isfinite(silence_timeout) or
                not 0 < heartbeat_interval < silence_timeout <= 120):
            raise ValueError("heartbeat and silence limits must be finite and ordered")
        if type(queue_bytes) is not int or not 1024 <= queue_bytes <= 8 * 1024 * 1024:
            raise ValueError("queue_bytes must be between 1024 and 8388608")
        public_key = signer.public_key_bytes()
        if type(public_key) is not bytes or len(public_key) != 32:
            raise ValueError("signer must supply a full Ed25519 public key")
        if not callable(getattr(clock, "time", None)):
            raise TypeError("clock must be supplied explicitly")
        self._signer, self._clock = signer, clock
        self._own_key = public_key.hex()
        self._allow_forwarding = allow_forwarding
        self._heartbeat_interval, self._silence_timeout = heartbeat_interval, silence_timeout
        self._queue_limit = queue_bytes
        self._inbound_bytes = 0
        self._sent_messages = 0
        self._received_messages = 0
        self._lock = threading.RLock()
        self._links: dict[str, _Link] = {}
        self._used_ids: set[str] = set()
        self._callbacks = {"message": [], "connect": [], "disconnect": [], "error": []}
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        self._close_task: asyncio.Task | None = None
        self._last_error: str | None = None
        self._cleanup_failed = False
        self._worker = MessageWorker(self._deliver_message, maxsize=128,
                                     on_error=self._handler_failed,
                                     name="poker-direct-dispatch-" + uuid.uuid4().hex[:8])
        self._worker.start()

    def get_clock(self):
        return self._clock

    def peer_ids(self) -> list[str]:
        with self._lock:
            return [cid for cid, link in self._links.items() if not link.closing]

    def _register(self, event, callback) -> None:
        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._lock:
            if self._closed:
                raise DirectTransportError("transport is closed")
            if len(self._callbacks[event]) >= 32:
                raise DirectTransportError("callback limit exceeded")
            self._callbacks[event].append(callback)

    def on_message(self, callback) -> None:
        self._register("message", callback)

    def on_connect(self, callback) -> None:
        self._register("connect", callback)

    def on_disconnect(self, callback, *, with_reason=False) -> None:
        if not callable(callback):
            raise TypeError("callback must be callable")
        if type(with_reason) is not bool:
            raise TypeError("with_reason must be a bool")
        self._register("disconnect", callback if with_reason else lambda cid, _reason: callback(cid))

    def on_task_error(self, callback) -> None:
        self._register("error", callback)

    def reset_callbacks(self) -> None:
        with self._lock:
            for callbacks in self._callbacks.values():
                callbacks.clear()

    def dispatch_work(self, callback, *args) -> bool:
        """Schedule host commands/ticks on the same off-loop protocol owner."""
        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._lock:
            if self._closed:
                return False
        return self._worker.submit_event(callback, *args)

    def _emit(self, kind, *args) -> None:
        with self._lock:
            callbacks = tuple(self._callbacks[kind]) if not self._closed else ()
        for callback in callbacks:
            callback(*args)

    def _handler_failed(self, _error) -> None:
        # This runs on the existing callback owner, not the socket loop.
        with self._lock:
            self._last_error = "handler_failed"
            callbacks = tuple(self._callbacks["error"])
            connections = list(self._links)
        for cid in connections:
            self.disconnect(cid, reason="protocol")
        for callback in callbacks:
            try:
                callback(DirectTransportError("message handler failed"))
            except Exception:
                pass

    def _deliver_message(self, conn_id, item) -> None:
        message, size = item
        try:
            self._emit("message", conn_id, message)
        finally:
            with self._lock:
                self._inbound_bytes -= size

    def _check_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("transport must stay on its creating asyncio loop")

    def _task(self, coroutine, link=None, name="work"):
        task = self._loop.create_task(coroutine, name="poker-direct-" + name)
        self._tasks.add(task)
        if link is not None:
            link.tasks.add(task)
        def complete(done):
            self._tasks.discard(done)
            if link is not None:
                link.tasks.discard(done)
            if not done.cancelled():
                error = done.exception()
                if error is not None:
                    with self._lock:
                        self._last_error = "task_failed"
                        self._cleanup_failed = True
                    if not self._closed:
                        self._worker.submit_event(self._handler_failed, error)
        task.add_done_callback(complete)
        return task

    async def attach_peer(self, peer: DirectPeer, *, remote_key: bytes,
                          conn_id: str | None = None,
                          remote_can_forward: bool = False) -> str:
        """Take ownership after signed setup and channel opening, never before."""
        self._check_loop()
        if type(remote_key) is not bytes or len(remote_key) != 32 or remote_key.hex() == self._own_key:
            raise ValueError("a different full remote public key is required")
        if type(remote_can_forward) is not bool:
            raise ValueError("remote_can_forward must be a bool")
        conn_id = uuid.uuid4().hex if conn_id is None else conn_id
        if type(conn_id) is not str or not 1 <= len(conn_id) <= 128:
            raise ValueError("invalid connection identifier")
        await peer.wait_open(timeout=1)
        with self._lock:
            if self._closed:
                raise DirectTransportError("transport is closed")
            if (len(self._links) >= 8 or len(self._used_ids) >= 256 or
                    conn_id in self._used_ids or
                    any(link.remote_key == remote_key.hex() for link in self._links.values())):
                raise DirectTransportError("duplicate connection or peer limit exceeded")
            link = _Link(conn_id, peer, remote_key.hex(), remote_can_forward)
            self._links[conn_id] = link
            self._used_ids.add(conn_id)
        if not self._worker.submit_event(self._emit, "connect", conn_id, "direct"):
            await self._drop(link, "protocol")
            raise DirectTransportError("connection callback queue refused work")
        self._task(self._receive(link), link, "receive")
        self._task(self._heartbeat(link), link, "heartbeat")
        return conn_id

    def _encode(self, message: dict) -> bytes:
        if type(message) is not dict:
            raise TypeError("message must be a dict")
        if "sig" in message:
            raw = json.dumps(message, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            verified = wire.unpack(raw, clock=self._clock)
            if verified["pubkey"] != self._own_key and not self._allow_forwarding:
                raise ValueError("only the host adapter can forward another signature")
        else:
            payload = message.get("payload", {})
            payload = {} if payload is None else payload
            if type(payload) is not dict:
                raise TypeError("payload must be a dict")
            payload = dict(payload)
            for key, value in message.items():
                if key not in ("type", "payload"):
                    if key in payload and payload[key] != value:
                        raise ValueError("conflicting message field")
                    payload[key] = value
            raw = wire.pack(message.get("type", ""), payload, signer=self._signer, clock=self._clock)
        if len(raw) > wire.MAX_JSON_BYTES:
            raise ValueError("signed message exceeds the wire bound")
        return raw

    def send(self, conn_id: str, message: dict) -> None:
        self._send_bytes(conn_id, self._encode(message))

    def broadcast(self, message: dict) -> None:
        self.broadcast_except(None, message)

    def broadcast_except(self, exclude_conn_id, message: dict) -> None:
        raw = self._encode(message)
        for conn_id in self.peer_ids():
            if conn_id != exclude_conn_id:
                self._send_bytes(conn_id, raw)

    def _send_bytes(self, conn_id, raw):
        with self._lock:
            link = self._links.get(conn_id)
            if self._closed or link is None or link.closing:
                # The socket loop can remove a peer after the protocol
                # worker snapshots recipients, or before a queued reply.
                # Its disconnect event already owns that transition;
                # turning the raced send into a handler failure would
                # disconnect every surviving peer as well.
                return
            if (link.queued_messages >= _MAX_QUEUED_ENVELOPES or
                    link.queued_bytes + len(raw) > min(self._queue_limit, 2 * wire.MAX_JSON_BYTES)):
                self._loop.call_soon_threadsafe(self._request_drop, link, "protocol")
                raise DirectTransportError("outgoing queue limit exceeded")
            link.queued_messages += 1
            link.queued_bytes += len(raw)
            link.outgoing.append(raw)
        self._loop.call_soon_threadsafe(self._start_send, link)

    def _start_send(self, link):
        if (link.closing or self._closed or
                (link.send_task is not None and not link.send_task.done())):
            return
        link.send_task = self._task(self._send(link), link, "send")

    async def _send(self, link):
        # A deal can enqueue many envelopes in one protocol callback. One
        # bounded FIFO absorbs that burst without creating concurrent native
        # sends or exceeding DirectPeer's own pending-send limit.
        while True:
            with self._lock:
                if self._closed or link.closing or not link.outgoing:
                    return
                raw = link.outgoing.popleft()
            try:
                await link.peer.send(raw)
                with self._lock:
                    self._sent_messages += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self._request_drop(link, "disconnect")
                return
            finally:
                with self._lock:
                    link.queued_messages -= 1
                    link.queued_bytes -= len(raw)

    async def _heartbeat(self, link):
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                self._send_bytes(link.conn_id, self._encode({"type": "heartbeat"}))
        except asyncio.CancelledError:
            pass
        except Exception:
            self._request_drop(link, "disconnect")

    async def _receive(self, link):
        reason = "disconnect"
        try:
            while True:
                raw = await link.peer.receive(timeout=self._silence_timeout)
                message = wire.unpack(raw, clock=self._clock)
                if message["type"] == "heartbeat":
                    if message["pubkey"] != link.remote_key:
                        raise ValueError("heartbeat key differs from authenticated setup")
                    with self._lock:
                        self._received_messages += 1
                    continue
                if not link.remote_can_forward and message["pubkey"] != link.remote_key:
                    raise ValueError("message key differs from authenticated setup")
                with self._lock:
                    self._received_messages += 1
                    if self._inbound_bytes + len(raw) > self._queue_limit:
                        raise ValueError("incoming byte queue limit exceeded")
                    self._inbound_bytes += len(raw)
                if not self._worker.submit(link.conn_id, (message, len(raw))):
                    with self._lock:
                        self._inbound_bytes -= len(raw)
                    raise ValueError("incoming callback queue refused work")
        except asyncio.CancelledError:
            return
        except asyncio.TimeoutError:
            reason = "heartbeat"
        except wire.ClockWindowError:
            reason = "clock"
        except (ValueError, TypeError, KeyError):
            reason = "protocol"
        except Exception:
            reason = "disconnect"
        self._request_drop(link, reason)

    def disconnect(self, conn_id: str, *, reason="disconnect") -> None:
        if reason not in ("disconnect", "heartbeat", "clock", "protocol"):
            raise ValueError("unknown disconnect reason")
        with self._lock:
            link = self._links.get(conn_id)
        if link is not None:
            self._loop.call_soon_threadsafe(self._request_drop, link, reason)

    def _request_drop(self, link, reason):
        if link.close_task is None:
            link.close_task = self._task(self._drop(link, reason), name="close-peer")
        return link.close_task

    async def _drop(self, link, reason):
        with self._lock:
            link.closing = True
        current = asyncio.current_task()
        tasks = [task for task in link.tasks if task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with self._lock:
            link.outgoing.clear()
            link.queued_messages = 0
            link.queued_bytes = 0
        try:
            await link.peer.close()
        except Exception:
            with self._lock:
                self._last_error = "peer_cleanup_failed"
                self._cleanup_failed = True
            reason = "protocol"
        with self._lock:
            self._links.pop(link.conn_id, None)
        if not self._closed and not self._worker.submit_event(self._emit, "disconnect", link.conn_id, reason):
            self._handler_failed(None)

    async def aclose(self) -> None:
        """Close owned peers/tasks/worker without stopping the containing loop."""
        self._check_loop()
        with self._lock:
            self._closed = True
        if self._close_task is None:
            self._close_task = self._loop.create_task(self._close_all(), name="poker-direct-close-all")
        await asyncio.shield(self._close_task)

    async def drain(self, timeout=10.0) -> None:
        """Wait for locally queued sends; application receipts prove delivery."""
        self._check_loop()
        async def wait():
            while True:
                with self._lock:
                    if self._closed or self._last_error is not None:
                        raise DirectTransportError("transport cannot drain")
                    if not any(link.queued_messages for link in self._links.values()):
                        return
                await asyncio.sleep(.01)
        await asyncio.wait_for(wait(), timeout)

    async def _close_all(self):
        with self._lock:
            links = list(self._links.values())
        await asyncio.gather(*(self._request_drop(link, "disconnect") for link in links))
        await asyncio.sleep(0)  # run task completion and queued-send callbacks
        self._worker.stop(timeout=5)
        if self._worker.is_alive():
            raise DirectTransportError("callback worker did not stop")
        with self._lock:
            self._inbound_bytes = 0
        if self._cleanup_failed:
            raise DirectTransportError("owned peer cleanup failed")

    def diagnostics(self) -> dict:
        with self._lock:
            return {"peers": len(self._links), "closed": self._closed,
                    "sent_messages": self._sent_messages, "received_messages": self._received_messages,
                    "queued_inbound_bytes": self._inbound_bytes,
                    "owned_tasks_pending": sum(not task.done() for task in self._tasks),
                    "callback_worker_alive": self._worker.is_alive(),
                    "last_error": self._last_error}
