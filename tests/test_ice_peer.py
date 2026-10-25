"""Direct UDP gate: real local WebRTC peers and bounded hostile framing.

Local tests disable STUN entirely. They prove actual SCTP/DTLS/UDP traffic,
not Internet reachability; the packaged separate-network gate owns that proof.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from holdem import ice_peer
from holdem.ice_peer import DirectPeer, DirectPeerError

HAS_AIORTC = importlib.util.find_spec("aiortc") is not None
requires_aiortc = pytest.mark.skipif(
    not HAS_AIORTC, reason="optional holdem[internet] extra is not installed")


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=35))


async def connected():
    left = DirectPeer(stun_urls=())
    right = DirectPeer(stun_urls=())
    try:
        offer = await left.create_offer()
        answer = await right.accept_offer(offer)
        await left.accept_answer(answer)
        await asyncio.gather(left.wait_open(timeout=10), right.wait_open(timeout=10))
        return left, right
    except BaseException:
        await asyncio.gather(left.close(), right.close())
        raise


def test_module_import_does_not_require_optional_dependency():
    script = """
import builtins, sys
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'aiortc' or name.startswith(('aiortc.', 'aioice', 'av')):
        raise AssertionError('optional dependency imported eagerly')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import holdem.ice_peer
assert 'aiortc' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-I", "-c", script, str(REPO)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@requires_aiortc
def test_real_peers_exchange_full_signed_envelope_size_and_empty_message():
    async def scenario():
        left, right = await connected()
        try:
            payload = bytes(range(256)) * 2048  # Exactly 512 KiB, not base64.
            await left.send(payload)
            assert await right.receive(timeout=10) == payload
            await right.send(b"")
            assert await left.receive(timeout=10) == b""
            await asyncio.gather(left.send(b"left"), right.send(b"right"))
            assert await left.receive(timeout=10) == b"right"
            assert await right.receive(timeout=10) == b"left"
            route = left.diagnostics()
            assert route["state"] == "connected"
            assert route["selected_pair"] == {
                "protocol": "udp", "local_type": "host", "remote_type": "host"}
            assert route["local_candidate_kinds"] == ["host"]
            assert route["remote_candidate_kinds"] == ["host"]
            assert "address" not in json.dumps(route)
            assert "local_address" in left.diagnostics(private=True)["selected_pair"]
        finally:
            await asyncio.gather(left.close(), right.close())
        assert left._close_task.done() and right._close_task.done()
        assert left._pc.connectionState == right._pc.connectionState == "closed"
        assert left._partial_timer is right._partial_timer is None

    run(scenario())


@requires_aiortc
def test_real_peer_rejects_nonbinary_frame_and_wakes_waiting_receive():
    async def scenario():
        left, right = await connected()
        try:
            receive = asyncio.create_task(right.receive(timeout=10))
            left._channel.send("an unframed text message")
            with pytest.raises(DirectPeerError, match="invalid binary frame"):
                await receive
            await asyncio.wait_for(asyncio.shield(right._close_task), timeout=5)
            assert right._pc.connectionState == "closed"
        finally:
            await asyncio.gather(left.close(), right.close())
    run(scenario())


@requires_aiortc
def test_real_peer_rejects_extra_channel_instead_of_accepting_it():
    async def scenario():
        left, right = await connected()
        try:
            left._pc.createDataChannel("unapproved", protocol="other")
            with pytest.raises(DirectPeerError, match="unexpected extra"):
                await right.receive(timeout=10)
        finally:
            await asyncio.gather(left.close(), right.close())
    run(scenario())


@requires_aiortc
@pytest.mark.parametrize("frame", [
    b"short",
    b"x" * (ice_peer.MAX_FRAME_BYTES + 1),
    struct.pack("!4sIII", b"oops", 1, 0, 0),
    struct.pack("!4sIII", b"HPD1", 2, 0, 0),
    struct.pack("!4sIII", b"HPD1", 1, 524289, 0),
    struct.pack("!4sIII", b"HPD1", 1, 1, 1),
    struct.pack("!4sIII", b"HPD1", 1, 1, 0),
    struct.pack("!4sIII", b"HPD1", 1, 0, 0) + b"extra",
])
def test_invalid_frames_close_and_release_buffers(frame):
    async def scenario():
        peer = DirectPeer(stun_urls=())
        try:
            peer._on_frame(frame)
            with pytest.raises(DirectPeerError):
                await peer.receive(timeout=0.1)
            assert peer._partial is None
            assert peer._partial_timer is None
            assert peer._queued_bytes == 0
        finally:
            await peer.close()
    run(scenario())


@requires_aiortc
def test_fragment_total_cannot_change_mid_message():
    async def scenario():
        peer = DirectPeer(stun_urls=())
        size = ice_peer._FRAME_PAYLOAD
        try:
            peer._on_frame(ice_peer._HEADER.pack(b"HPD1", 1, size + 2, 0) + b"a" * size)
            assert peer._partial is not None
            peer._on_frame(ice_peer._HEADER.pack(b"HPD1", 1, size + 1, size) + b"b")
            with pytest.raises(DirectPeerError, match="fragment sequence"):
                await peer.receive(timeout=0.1)
            assert peer._partial is None
        finally:
            await peer.close()
    run(scenario())


@requires_aiortc
def test_incomplete_message_expires_even_without_more_traffic(monkeypatch):
    monkeypatch.setattr(ice_peer, "_INCOMPLETE_TIMEOUT", 0.02)
    async def scenario():
        peer = DirectPeer(stun_urls=())
        try:
            size = ice_peer._FRAME_PAYLOAD
            peer._on_frame(ice_peer._HEADER.pack(b"HPD1", 1, size + 1, 0) + b"a" * size)
            with pytest.raises(DirectPeerError, match="incomplete message expired"):
                await peer.receive(timeout=1)
            assert peer._partial is None
            assert peer._partial_timer is None
        finally:
            await peer.close()
    run(scenario())


@requires_aiortc
def test_slow_consumer_has_both_byte_and_message_caps():
    async def scenario():
        peer = DirectPeer(stun_urls=(), max_message_bytes=1)
        try:
            for sequence in range(1, 4):
                peer._on_frame(ice_peer._HEADER.pack(b"HPD1", sequence, 1, 0) + b"a")
            with pytest.raises(DirectPeerError, match="inbound queue"):
                await peer.receive(timeout=0.1)
            assert peer._queued_bytes == 0
        finally:
            await peer.close()
        peer = DirectPeer(stun_urls=())
        try:
            for sequence in range(1, ice_peer._QUEUE_MESSAGES + 2):
                peer._on_frame(ice_peer._HEADER.pack(b"HPD1", sequence, 0, 0))
            with pytest.raises(DirectPeerError, match="inbound queue"):
                await peer.receive(timeout=0.1)
        finally:
            await peer.close()
    run(scenario())


@requires_aiortc
def test_close_unblocks_pending_receive_and_open_and_is_idempotent():
    async def scenario():
        peer = DirectPeer(stun_urls=())
        receiving = asyncio.create_task(peer.receive(timeout=10))
        opening = asyncio.create_task(peer.wait_open(timeout=10))
        await asyncio.sleep(0)
        await peer.close()
        for pending in (receiving, opening):
            with pytest.raises(DirectPeerError, match="closed"):
                await pending
        await peer.close()
        assert not [task for task in asyncio.all_tasks()
                    if task is not asyncio.current_task() and
                    task.get_name() == "holdem-direct-peer-close" and not task.done()]
    run(scenario())


@requires_aiortc
def test_send_failure_is_visible_and_does_not_leave_reserved_bytes(monkeypatch):
    async def scenario():
        left, right = await connected()
        try:
            def broken_send(_frame):
                raise OSError("synthetic native send failure")
            monkeypatch.setattr(left._channel, "send", broken_send)
            with pytest.raises(OSError, match="synthetic native send failure"):
                await left.send(b"must not report success")
            assert left._pending_send_bytes == left._pending_send_count == 0
            with pytest.raises(DirectPeerError, match="send failed"):
                await left.send(b"retry")
        finally:
            await asyncio.gather(left.close(), right.close())
    run(scenario())


@requires_aiortc
def test_outbound_backpressure_timeout_closes_partial_send(monkeypatch):
    monkeypatch.setattr(ice_peer, "_SEND_TIMEOUT", 0.02)
    async def scenario():
        left, right = await connected()
        try:
            monkeypatch.setattr(type(left._channel), "bufferedAmount",
                                property(lambda _self: ice_peer._BUFFER_HIGH))
            with pytest.raises(TimeoutError):
                await left.send(b"stalled")
            assert left._pending_send_bytes == left._pending_send_count == 0
            assert left._stopped
        finally:
            await asyncio.gather(left.close(), right.close())
    run(scenario())


@requires_aiortc
def test_concurrent_senders_are_ordered_and_have_a_bounded_wait_queue():
    async def scenario():
        left, right = await connected()
        try:
            await left._send_lock.acquire()
            queued = [asyncio.create_task(left.send(bytes([value])))
                      for value in range(ice_peer._QUEUE_MESSAGES)]
            # Let each send reserve its bounded slot before checking overflow.
            await asyncio.sleep(0)
            assert left._pending_send_count == ice_peer._QUEUE_MESSAGES
            with pytest.raises(DirectPeerError, match="outbound queue"):
                await left.send(b"one too many")
            left._send_lock.release()
            await asyncio.gather(*queued)
            assert [await right.receive(timeout=5) for _ in queued] == [
                bytes([value]) for value in range(ice_peer._QUEUE_MESSAGES)]
            assert left._pending_send_count == left._pending_send_bytes == 0
        finally:
            if left._send_lock.locked():
                left._send_lock.release()
            await asyncio.gather(left.close(), right.close())
    run(scenario())


@requires_aiortc
def test_close_releases_each_owned_udp_socket_and_leaves_no_native_tasks():
    async def scenario():
        before = asyncio.all_tasks()
        left, right = await connected()
        sockets = [transport.get_extra_info("socket")
                   for peer in (left, right)
                   for transport in ice_peer._owned_datagrams(peer._pc)]
        assert sockets and all(sock.fileno() >= 0 for sock in sockets)
        # The real malformed-frame path closes while an ACK write is pending,
        # reproducing the Proactor close bug which normal happy-path misses.
        left._channel.send("bad")
        with pytest.raises(DirectPeerError):
            await right.receive(timeout=5)
        await asyncio.gather(left.close(), right.close())
        await asyncio.sleep(0)
        assert all(sock.fileno() == -1 for sock in sockets)
        pending = [task for task in asyncio.all_tasks() - before if not task.done()]
        assert not pending, [repr(task) for task in pending]
    run(scenario())


@requires_aiortc
@pytest.mark.parametrize("cancel_caller", [False, True])
def test_close_during_stun_gather_owns_and_finishes_native_children(cancel_caller):
    async def scenario():
        before = asyncio.all_tasks()
        # Local UDP discard: exercises the real STUN timeout without depending
        # on a public service, router change, or working Internet connection.
        peer = DirectPeer(stun_urls=("stun:127.0.0.1:9",))
        offer = asyncio.create_task(peer.create_offer())
        while not ice_peer._owned_datagrams(peer._pc):
            await asyncio.sleep(0)
        sockets = [transport.get_extra_info("socket")
                   for transport in ice_peer._owned_datagrams(peer._pc)]
        if cancel_caller:
            offer.cancel()
        await peer.close()
        result = (await asyncio.gather(offer, return_exceptions=True))[0]
        assert isinstance(result, (DirectPeerError, asyncio.CancelledError))
        await asyncio.sleep(0)
        assert peer._setup_task.done()
        assert all(sock.fileno() == -1 for sock in sockets)
        assert not [task for task in asyncio.all_tasks() - before if not task.done()]
    run(scenario())


@requires_aiortc
@pytest.mark.parametrize("urls", [("turn:example.org:3478",),
                                  ("stuns:example.org:5349",),
                                  ("stun:user@example.org:3478",),
                                  ["stun:example.org:3478"]])
def test_only_stun_urls_can_be_configured(urls):
    async def scenario():
        with pytest.raises(ValueError, match="stun: URLs"):
            DirectPeer(stun_urls=urls)
    run(scenario())


@requires_aiortc
def test_relay_and_tcp_remote_candidates_rejected_before_native_setup():
    async def scenario():
        peer = DirectPeer(stun_urls=())
        try:
            offer = await peer.create_offer()
            for candidate in (
                "a=candidate:1 1 udp 1 203.0.113.1 40000 typ relay",
                "a=candidate:1 1 tcp 1 203.0.113.1 40000 typ host tcptype passive",
            ):
                other = DirectPeer(stun_urls=())
                try:
                    with pytest.raises(ValueError, match="direct UDP"):
                        await other.accept_offer({"type": "offer", "sdp": offer["sdp"] +
                                                  candidate + "\r\n"})
                    assert not other._setup_started
                finally:
                    await other.close()
        finally:
            await peer.close()
    run(scenario())


@requires_aiortc
def test_message_limit_prevents_silent_truncation():
    async def scenario():
        left, right = await connected()
        try:
            with pytest.raises(ValueError, match="configured limit"):
                await left.send(b"x" * (512 * 1024 + 1))
            await left.send(b"still usable")
            assert await right.receive(timeout=10) == b"still usable"
            with pytest.raises(TimeoutError):
                await right.receive(timeout=0.01)
            await left.send(b"after receive timeout")
            assert await right.receive(timeout=10) == b"after receive timeout"
        finally:
            await asyncio.gather(left.close(), right.close())
    run(scenario())
