"""Optional, host-owned direct WebRTC byte connection.

This module has no poker or signalling policy. The host must authenticate the
offer/answer before passing them here. STUN discovers routes; TURN and relay
candidates are deliberately rejected. Importing the module does not import
aiortc, open sockets, or start tasks. Construct and use a peer on one running
asyncio loop, and always await ``close()``.

``send`` means that the bounded local data-channel buffer drained successfully,
not that the other application accepted a message. Applications needing proof
of receipt must exchange their own acknowledgements.
"""
from __future__ import annotations

import asyncio
import math
import struct
from typing import Any


CHANNEL_LABEL = "holdem-direct-v1"
CHANNEL_PROTOCOL = "holdem.direct/1"
MAX_FRAME_BYTES = 16 * 1024
_HEADER = struct.Struct("!4sIII")
_MAGIC = b"HPD1"
_FRAME_PAYLOAD = MAX_FRAME_BYTES - _HEADER.size
_QUEUE_MESSAGES = 8
_BUFFER_HIGH = 256 * 1024
_INCOMPLETE_TIMEOUT = 15.0
_SEND_TIMEOUT = 30.0
_CLOSE_GRACE = 1.0
_CLOSE_ABORT_TIMEOUT = 3.0
_SETUP_CLEANUP_TIMEOUT = 15.0
_MAX_SDP_BYTES = 128 * 1024


class DirectPeerError(ConnectionError):
    """The direct connection failed or exceeded its bounded protocol."""


class DirectPeer:
    """One reliable, ordered binary channel with bounded message framing.

    The optional ``internet`` extra supplies the pinned aiortc implementation.
    Empty ``stun_urls`` is useful for offline/local acceptance tests. No public
    endpoint or TURN service is contacted when that tuple is empty.
    """

    def __init__(self, *,
                 stun_urls: tuple[str, ...] = ("stun:stun.l.google.com:19302",),
                 max_message_bytes: int = 512 * 1024) -> None:
        self._loop = asyncio.get_running_loop()
        if type(max_message_bytes) is not int or not 1 <= max_message_bytes <= 512 * 1024:
            raise ValueError("max_message_bytes must be between 1 and 524288")
        if (not isinstance(stun_urls, tuple) or len(stun_urls) > 4 or
                any(not isinstance(url, str) or len(url) > 256 or
                    not url.startswith("stun:") or "@" in url or
                    any(c.isspace() for c in url) or len(url) <= 5
                    for url in stun_urls)):
            raise ValueError("only a bounded tuple of stun: URLs is allowed")
        try:
            from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection
        except ImportError as exc:
            raise RuntimeError("Direct Internet play requires holdem[internet]") from exc

        self._max_message_bytes = max_message_bytes
        self._queue_byte_limit = 2 * max_message_bytes
        self._pending_send_bytes = 0
        self._pending_send_count = 0
        self._send_lock = asyncio.Lock()
        self._receive_lock = asyncio.Lock()
        self._opened = asyncio.Event()
        self._received = asyncio.Event()
        self._writable = asyncio.Event()
        self._writable.set()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_QUEUE_MESSAGES)
        self._queued_bytes = 0
        self._partial: bytearray | None = None
        self._partial_total = 0
        self._partial_timer: asyncio.TimerHandle | None = None
        self._send_id = 1
        self._receive_id = 1
        self._stopped = False
        self._error: DirectPeerError | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._setup_task: asyncio.Task | None = None
        self._setup_started = False
        self._pc = RTCPeerConnection(RTCConfiguration(
            iceServers=[RTCIceServer(urls=list(stun_urls))] if stun_urls else []))
        # Both endpoints allocate the same channel; arbitrary in-band channels
        # are never accepted. Defaults are fully reliable, with no lifetime or
        # retransmit cap. A single sending lock prevents fragment interleaving.
        self._channel = self._pc.createDataChannel(
            CHANNEL_LABEL, protocol=CHANNEL_PROTOCOL, ordered=True,
            negotiated=True, id=0)
        self._channel.bufferedAmountLowThreshold = 0

        @self._channel.on("open")
        def channel_open() -> None:
            if not self._stopped:
                self._opened.set()

        @self._channel.on("bufferedamountlow")
        def channel_writable() -> None:
            self._writable.set()

        @self._channel.on("message")
        def channel_message(frame: Any) -> None:
            self._on_frame(frame)

        @self._channel.on("close")
        def channel_closed() -> None:
            if not self._stopped:
                self._fail("data channel closed")

        @self._pc.on("datachannel")
        def extra_channel(channel: Any) -> None:
            channel.close()
            self._fail("unexpected extra data channel")

        @self._pc.on("connectionstatechange")
        def connection_changed() -> None:
            if self._pc.connectionState in ("failed", "closed") and not self._stopped:
                self._fail("peer connection " + self._pc.connectionState)

    def _check_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("DirectPeer must stay on its creating asyncio loop")

    def _check_running(self) -> None:
        self._check_loop()
        if self._error is not None:
            raise self._error
        if self._stopped:
            raise DirectPeerError("direct peer is closed")

    @staticmethod
    def _description(description: dict, expected_type: str) -> Any:
        if (not isinstance(description, dict) or set(description) != {"type", "sdp"}
                or description.get("type") != expected_type
                or not isinstance(description.get("sdp"), str)
                or len(description["sdp"].encode("utf-8")) > _MAX_SDP_BYTES):
            raise ValueError("invalid bounded " + expected_type + " description")
        sdp = description["sdp"]
        media = [line for line in sdp.splitlines() if line.startswith("m=")]
        if len(media) != 1 or not media[0].startswith("m=application "):
            raise ValueError("description must contain exactly one data transport")
        for line in sdp.splitlines():
            if line.startswith("a=candidate:"):
                fields = line.split()
                if (len(fields) < 8 or fields[2].lower() != "udp" or
                        fields[6] != "typ" or fields[7] not in ("host", "srflx", "prflx")):
                    raise ValueError("only direct UDP ICE candidates are allowed")
        from aiortc import RTCSessionDescription
        return RTCSessionDescription(sdp=sdp, type=expected_type)

    async def create_offer(self) -> dict:
        self._check_running()
        if self._setup_started:
            raise DirectPeerError("connection setup already started")
        self._setup_started = True
        async def negotiate() -> dict:
            await self._pc.setLocalDescription(await self._pc.createOffer())
            return {"type": self._pc.localDescription.type,
                    "sdp": self._pc.localDescription.sdp}
        return await self._negotiate(negotiate(), "offer setup failed")

    async def accept_offer(self, description: dict) -> dict:
        self._check_running()
        if self._setup_started:
            raise DirectPeerError("connection setup already started")
        remote = self._description(description, "offer")
        self._setup_started = True
        async def negotiate() -> dict:
            await self._pc.setRemoteDescription(remote)
            await self._pc.setLocalDescription(await self._pc.createAnswer())
            return {"type": self._pc.localDescription.type,
                    "sdp": self._pc.localDescription.sdp}
        return await self._negotiate(negotiate(), "answer setup failed")

    async def accept_answer(self, description: dict) -> None:
        self._check_running()
        if self._pc.signalingState != "have-local-offer":
            raise DirectPeerError("no outstanding local offer")
        remote = self._description(description, "answer")
        await self._negotiate(self._pc.setRemoteDescription(remote), "remote answer failed")

    async def _negotiate(self, operation: Any, failure: str) -> Any:
        # Shield native gathering from caller cancellation. aioice's five-second
        # gather normally cancels its own STUN children; cancelling it midway
        # strands those children. close() owns and awaits this task before closing
        # its sockets, including when the host cancels the public setup call.
        if self._setup_task is not None and not self._setup_task.done():
            operation.close()
            raise DirectPeerError("connection setup operation already active")
        self._setup_task = self._loop.create_task(operation, name="holdem-direct-setup")
        try:
            result = await asyncio.shield(self._setup_task)
            self._check_running()
            return result
        except BaseException:
            self._fail(failure)
            raise

    async def wait_open(self, timeout: float = 30) -> None:
        self._check_running()
        await asyncio.wait_for(self._opened.wait(), timeout=timeout)
        self._check_running()
        if self._channel.readyState != "open":
            raise DirectPeerError("data channel did not open")

    async def send(self, payload: bytes) -> None:
        self._check_running()
        if type(payload) is not bytes:
            raise TypeError("payload must be bytes")
        if len(payload) > self._max_message_bytes:
            raise ValueError("message exceeds configured limit")
        if self._channel.readyState != "open":
            raise DirectPeerError("data channel is not open")
        if (self._pending_send_count >= _QUEUE_MESSAGES or
                self._pending_send_bytes + len(payload) > self._queue_byte_limit):
            raise DirectPeerError("outbound queue limit exceeded")
        self._pending_send_count += 1
        self._pending_send_bytes += len(payload)
        try:
            await asyncio.wait_for(self._send_frames(payload), timeout=_SEND_TIMEOUT)
        except asyncio.CancelledError:
            self._fail("send cancelled before completion")
            raise
        except Exception:
            self._fail("data channel send failed")
            raise
        finally:
            self._pending_send_count -= 1
            self._pending_send_bytes -= len(payload)

    async def _send_frames(self, payload: bytes) -> None:
        async with self._send_lock:
            self._check_running()
            if self._send_id > 0xffffffff:
                raise DirectPeerError("message sequence exhausted")
            message_id = self._send_id
            self._send_id += 1
            count = max(1, math.ceil(len(payload) / _FRAME_PAYLOAD))
            for index in range(count):
                offset = index * _FRAME_PAYLOAD
                frame = (_HEADER.pack(_MAGIC, message_id, len(payload), offset) +
                         payload[offset:offset + _FRAME_PAYLOAD])
                await self._drain(_BUFFER_HIGH - len(frame))
                self._check_running()
                self._channel.send(frame)
            await self._drain(0)
            self._check_running()

    async def _drain(self, threshold: int) -> None:
        while self._channel.bufferedAmount > threshold:
            self._check_running()
            self._writable.clear()
            # No await separates the condition and clear; callbacks execute on
            # this same event loop, so a low-buffer notification cannot vanish.
            await self._writable.wait()
        self._check_running()

    def _on_frame(self, frame: Any) -> None:
        if self._stopped:
            return
        if type(frame) is not bytes or not _HEADER.size <= len(frame) <= MAX_FRAME_BYTES:
            self._fail("invalid binary frame size or type")
            return
        magic, message_id, total, offset = _HEADER.unpack_from(frame)
        data = frame[_HEADER.size:]
        have = len(self._partial) if self._partial is not None else 0
        if (magic != _MAGIC or message_id != self._receive_id or
                total > self._max_message_bytes or offset != have or
                len(data) != min(_FRAME_PAYLOAD, total - offset) or
                offset > total or
                (self._partial is not None and total != self._partial_total)):
            self._fail("invalid fragment sequence or message bound")
            return
        if self._partial is None:
            if self._queued_bytes + total > self._queue_byte_limit or self._queue.full():
                self._fail("inbound queue limit exceeded")
                return
            self._partial = bytearray()
            self._partial_total = total
            self._partial_timer = self._loop.call_later(
                _INCOMPLETE_TIMEOUT, self._fail, "incomplete message expired")
        self._partial.extend(data)
        if len(self._partial) == total:
            completed = bytes(self._partial)
            self._clear_partial()
            self._receive_id += 1
            self._queued_bytes += total
            self._queue.put_nowait(completed)
            self._received.set()

    async def receive(self, timeout: float = 30) -> bytes:
        self._check_running()

        async def take() -> bytes:
            async with self._receive_lock:
                while True:
                    self._check_running()
                    if not self._queue.empty():
                        payload = self._queue.get_nowait()
                        self._queued_bytes -= len(payload)
                        return payload
                    self._received.clear()
                    await self._received.wait()

        return await asyncio.wait_for(take(), timeout=timeout)

    def _clear_partial(self) -> None:
        if self._partial_timer is not None:
            self._partial_timer.cancel()
            self._partial_timer = None
        self._partial = None
        self._partial_total = 0

    def _finish(self) -> None:
        self._stopped = True
        self._clear_partial()
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queued_bytes = 0
        self._opened.set()
        self._received.set()
        self._writable.set()

    def _fail(self, reason: str) -> None:
        if self._stopped:
            return
        self._error = DirectPeerError(reason)
        self._finish()
        self._start_close()

    def _start_close(self) -> None:
        if self._close_task is None:
            self._close_task = self._loop.create_task(
                self._close_native(), name="holdem-direct-peer-close")

    async def _close_native(self) -> None:
        if self._setup_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._setup_task),
                                       timeout=_SETUP_CLEANUP_TIMEOUT)
            except asyncio.TimeoutError as exc:
                self._setup_task.cancel()
                await asyncio.gather(self._setup_task, return_exceptions=True)
                self._cleanup_error = exc
            except Exception:
                pass  # The setup caller already receives its original failure.
            # Allow the gatherer's cancelled STUN children to run finally blocks.
            await asyncio.sleep(0)
        # Snapshot only sockets belonging to this peer. Python 3.13's Windows
        # Proactor UDP close can strand connection_lost when a write is pending.
        # Give normal aiortc cleanup a grace period, then abort those transports
        # through their public API without cancelling aiortc midway through its
        # close state machine. The version-specific ownership accessor is below.
        datagrams = _owned_datagrams(self._pc)
        native = self._loop.create_task(self._pc.close(), name="holdem-direct-native-close")
        try:
            try:
                await asyncio.wait_for(asyncio.shield(native), timeout=_CLOSE_GRACE)
            except asyncio.TimeoutError:
                for transport in datagrams:
                    transport.abort()
                await asyncio.wait_for(asyncio.shield(native), timeout=_CLOSE_ABORT_TIMEOUT)
        except Exception as exc:
            for transport in datagrams:
                transport.abort()
            if not native.done():
                native.cancel()
            await asyncio.gather(native, return_exceptions=True)
            if self._error is None:
                self._error = DirectPeerError("native peer cleanup failed")
            # Retrieved by close(), while a failure callback may schedule this
            # before its owner gets control back. Do not leave task exceptions
            # unobserved merely because the connection already failed.
            self._cleanup_error = exc

    async def close(self) -> None:
        self._check_loop()
        self._finish()
        self._start_close()
        await asyncio.shield(self._close_task)
        if hasattr(self, "_cleanup_error"):
            raise DirectPeerError("native peer cleanup failed") from self._cleanup_error

    def diagnostics(self, private: bool = False) -> dict:
        """Route types are public; raw addresses require explicit opt-in.

        aiortc exposes gathered/remote candidates publicly but not the selected
        pair. The isolated observation helper below reads the pinned aiortc
        1.15 / aioice 0.10 implementation. Missing internals mean unknown, never
        a guessed direct-connection success. This helper affects reporting only.
        """
        self._check_loop()
        result: dict = {
            "state": self._pc.connectionState,
            "ice_state": self._pc.iceConnectionState,
            "channel_state": self._channel.readyState,
            "local_candidate_kinds": [], "remote_candidate_kinds": [],
            "selected_pair": None,
            "error": str(self._error) if self._error else None,
        }
        ice = self._pc.sctp.transport.transport if self._pc.sctp else None
        if ice is not None:
            local = ice.iceGatherer.getLocalCandidates()
            remote = ice.getRemoteCandidates()
            result["local_candidate_kinds"] = sorted({c.type for c in local})
            result["remote_candidate_kinds"] = sorted({c.type for c in remote})
            result["selected_pair"] = _selected_pair(ice, private=private)
            if private:
                result["local_candidates"] = [
                    {"type": c.type, "protocol": c.protocol,
                     "address": c.ip, "port": c.port} for c in local]
                result["remote_candidates"] = [
                    {"type": c.type, "protocol": c.protocol,
                     "address": c.ip, "port": c.port} for c in remote]
        return result


def _selected_pair(ice: Any, *, private: bool) -> dict | None:
    """Diagnostic-only adapter for pinned aiortc 1.15 / aioice 0.10 internals."""
    nominated = getattr(getattr(ice, "_connection", None), "_nominated", None)
    if not isinstance(nominated, dict) or not nominated:
        return None
    pair = nominated.get(1)
    local = getattr(pair, "local_candidate", None)
    remote = getattr(pair, "remote_candidate", None)
    if local is None or remote is None:
        return None
    result = {"protocol": local.transport.lower(),
              "local_type": local.type, "remote_type": remote.type}
    if private:
        result["local_address"] = local.host
        result["local_port"] = local.port
        result["remote_address"] = remote.host
        result["remote_port"] = remote.port
    return result


def _owned_datagrams(pc: Any) -> tuple:
    """Owned UDP handles, pinned aiortc 1.15 / aioice 0.10 cleanup boundary.

    There is no public aiortc API for these transport handles. Each entry comes
    from this exact peer's ICE connection, never an event-loop/global registry.
    The handles themselves use only DatagramTransport's public abort API.
    """
    ice = pc.sctp.transport.transport if pc.sctp else None
    protocols = getattr(getattr(ice, "_connection", None), "_protocols", ())
    return tuple(protocol.transport for protocol in protocols
                 if getattr(protocol, "transport", None) is not None)
