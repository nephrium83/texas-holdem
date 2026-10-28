"""Real direct channels preserve signed Session semantics and resource ownership."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

pytest.importorskip("aiortc", reason="install the optional Internet stack for direct transport tests")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from holdem.ice_peer import DirectPeer
from holdem.ice_transport import DirectSessionTransport, DirectTransportError
from holdem.p2p.identity import Identity
from holdem.p2p.timeout import FakeClock, RealClock
from holdem.p2p import wire


def identity():
    return Identity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())


async def until(condition, timeout=5):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("direct transport condition did not complete")
        await asyncio.sleep(0.01)


async def channels():
    left, right = DirectPeer(stun_urls=()), DirectPeer(stun_urls=())
    try:
        await left.accept_answer(await right.accept_offer(await left.create_offer()))
        await asyncio.gather(left.wait_open(10), right.wait_open(10))
        return left, right
    except BaseException:
        await asyncio.gather(left.close(), right.close())
        raise


def test_three_independent_channels_forward_original_guest_signature():
    async def run():
        baseline = set(asyncio.all_tasks())
        keys = [identity() for _ in range(3)]
        peers = [await channels(), await channels()]
        transports = [DirectSessionTransport(signer=k, clock=RealClock(), allow_forwarding=(i == 0))
                      for i, k in enumerate(keys)]
        received = [[], [], []]
        for i, transport in enumerate(transports):
            transport.on_message(lambda cid, message, i=i: received[i].append((cid, message)))
        try:
            await transports[0].attach_peer(peers[0][0], remote_key=keys[1].public_key_bytes(), conn_id="guest-b")
            await transports[1].attach_peer(peers[0][1], remote_key=keys[0].public_key_bytes(), conn_id="host",
                                            remote_can_forward=True)
            await transports[0].attach_peer(peers[1][0], remote_key=keys[2].public_key_bytes(), conn_id="guest-c")
            await transports[2].attach_peer(peers[1][1], remote_key=keys[0].public_key_bytes(), conn_id="host",
                                            remote_can_forward=True)
            transports[1].send("host", {"type": "action", "seat": 1, "payload": {"amount": 50}})
            await until(lambda: bool(received[0]))
            conn_id, message = received[0][0]
            assert conn_id == "guest-b" and message["pubkey"] == keys[1].public_key_bytes().hex()
            assert message["payload"] == {"seat": 1, "amount": 50}
            transports[0].broadcast_except(conn_id, message)
            await until(lambda: bool(received[2]))
            assert received[2][0][1] == message
            assert received[1] == []
            with pytest.raises(ValueError, match="only the host"):
                transports[2].send("host", message)
            # Closing one guest leaves the host's other channel independent.
            await transports[1].aclose()
            await until(lambda: transports[0].peer_ids() == ["guest-c"])
            transports[0].send("guest-c", {"type": "still-alive", "payload": {"ok": True}})
            await until(lambda: len(received[2]) == 2)
        finally:
            await asyncio.gather(*(transport.aclose() for transport in transports))
        await asyncio.sleep(0)
        assert not [task for task in asyncio.all_tasks() - baseline if not task.done()]
        assert all(t.diagnostics()["owned_tasks_pending"] == 0 for t in transports)
        assert all(not t.diagnostics()["callback_worker_alive"] for t in transports)
    asyncio.run(run())


def test_peer_departure_during_broadcast_keeps_surviving_channel(monkeypatch):
    async def run():
        keys = [identity(), identity(), identity()]
        peers = [await channels(), await channels()]
        transport = DirectSessionTransport(signer=keys[0], clock=RealClock(), allow_forwarding=True)
        errors = []
        transport.on_task_error(errors.append)
        entered, release = threading.Event(), threading.Event()
        peer_ids = transport.peer_ids

        def paused_recipients():
            recipients = peer_ids()
            entered.set()
            assert release.wait(timeout=3), "broadcast fixture was not released"
            return recipients

        try:
            for i in range(2):
                await transport.attach_peer(peers[i][0], remote_key=keys[i + 1].public_key_bytes(),
                                            conn_id=f"guest-{i}")
            monkeypatch.setattr(transport, "peer_ids", paused_recipients)
            transport.dispatch_work(transport.broadcast, {"type": "still-playing", "payload": {"hand": 2}})
            await until(entered.is_set)
            # Removal happens on the socket loop while the protocol worker
            # retains its earlier recipient snapshot.
            transport.disconnect("guest-0")
            await until(lambda: peer_ids() == ["guest-1"])
            release.set()
            message = wire.unpack(await peers[1][1].receive(timeout=3), clock=RealClock())
            assert message["type"] == "still-playing" and message["payload"] == {"hand": 2}
            # A reply already queued for the departed player is inert too.
            transport.send("guest-0", {"type": "late-reply"})
            assert errors == []
            assert peer_ids() == ["guest-1"]
            assert transport.diagnostics()["last_error"] is None
        finally:
            release.set()
            await transport.aclose()
            await asyncio.gather(*(pair[1].close() for pair in peers))
    asyncio.run(run())


@pytest.mark.parametrize("attack,reason", [("unsigned", "protocol"), ("signature", "protocol"),
                                           ("other-key", "protocol"), ("clock", "clock"),
                                           ("deep", "protocol")])
def test_hostile_or_stale_envelopes_close_before_session_delivery(attack, reason):
    async def run():
        left, right = await channels()
        local, remote = identity(), identity()
        transport = DirectSessionTransport(signer=local, clock=RealClock())
        messages, closed = [], []
        transport.on_message(lambda *args: messages.append(args))
        transport.on_disconnect(lambda cid, why: closed.append((cid, why)), with_reason=True)
        try:
            await transport.attach_peer(left, remote_key=remote.public_key_bytes(), conn_id="guest")
            if attack == "unsigned":
                raw = b'{"type":"action","payload":{}}'
            elif attack == "deep":
                raw = b'[' * 100 + b'0' + b']' * 100
            else:
                signer = identity() if attack == "other-key" else remote
                clock = FakeClock(start=1) if attack == "clock" else RealClock()
                raw = wire.pack("action", {}, signer=signer, clock=clock)
                if attack == "signature":
                    message = json.loads(raw)
                    message["sig"] = "00" * 64
                    raw = json.dumps(message).encode()
            await right.send(raw)
            await until(lambda: bool(closed))
            assert closed == [("guest", reason)] and messages == []
        finally:
            await transport.aclose()
            await right.close()
    asyncio.run(run())


@pytest.mark.parametrize("attack", ["payload", "timestamp", "type"])
def test_malformed_signed_message_cannot_interrupt_healthy_channel(attack):
    async def run():
        import hashlib
        keys = [identity(), identity(), identity()]
        peers = [await channels(), await channels()]
        transport = DirectSessionTransport(signer=keys[0], clock=RealClock(), allow_forwarding=True)
        messages, closed, errors = [], [], []
        transport.on_message(lambda cid, msg: messages.append((cid, msg)))
        transport.on_disconnect(lambda cid, why: closed.append((cid, why)), with_reason=True)
        transport.on_task_error(errors.append)
        try:
            for i, pair in enumerate(peers):
                await transport.attach_peer(pair[0], remote_key=keys[i + 1].public_key_bytes(), conn_id=f"guest-{i}")
            message = json.loads(wire.pack("chat", {"text": "hello"}, signer=keys[1], clock=RealClock()))
            message[{"payload": "payload", "timestamp": "ts", "type": "type"}[attack]] = None if attack == "payload" else []
            message.pop("sig")
            message.pop("hash")
            message["sig"] = keys[1].sign(json.dumps(message, sort_keys=True, separators=(",", ":")).encode()).hex()
            message["hash"] = hashlib.sha256(json.dumps(message, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            await peers[0][1].send(json.dumps(message).encode())
            await until(lambda: bool(closed))
            assert closed == [("guest-0", "protocol")]
            assert transport.peer_ids() == ["guest-1"] and messages == []
            raw = wire.pack("chat", {"text": "still playing"}, signer=keys[2], clock=RealClock())
            await peers[1][1].send(raw)
            await until(lambda: bool(messages))
            assert messages == [("guest-1", json.loads(raw))]
            assert errors == [] and transport.diagnostics()["last_error"] is None
        finally:
            await transport.aclose()
            await asyncio.gather(*(pair[1].close() for pair in peers))
        assert transport.diagnostics()["owned_tasks_pending"] == 0
        assert not transport.diagnostics()["callback_worker_alive"]
    asyncio.run(run())


def test_signed_heartbeats_keep_idle_peer_alive_and_silent_peer_expires():
    async def run():
        keys = [identity(), identity()]
        left, right = await channels()
        transports = [DirectSessionTransport(signer=k, clock=RealClock(), heartbeat_interval=.03, silence_timeout=.2)
                      for k in keys]
        try:
            await transports[0].attach_peer(left, remote_key=keys[1].public_key_bytes(), conn_id="other")
            await transports[1].attach_peer(right, remote_key=keys[0].public_key_bytes(), conn_id="other")
            await asyncio.sleep(.6)
            assert all(t.peer_ids() == ["other"] for t in transports)
            assert all(t.diagnostics()["sent_messages"] > 0 for t in transports)
            assert all(t.diagnostics()["received_messages"] > 0 for t in transports)
        finally:
            await asyncio.gather(*(t.aclose() for t in transports))
        left, right = await channels()
        transport = DirectSessionTransport(signer=keys[0], clock=RealClock(), heartbeat_interval=.03, silence_timeout=.15)
        disconnected = []
        transport.on_disconnect(lambda _, reason: disconnected.append(reason), with_reason=True)
        try:
            await transport.attach_peer(left, remote_key=keys[1].public_key_bytes())
            await until(lambda: bool(disconnected))
            assert disconnected == ["heartbeat"]
        finally:
            await transport.aclose()
            await right.close()
    asyncio.run(run())


def test_slow_callback_is_off_socket_loop_and_incoming_bytes_are_bounded():
    async def run():
        local, remote = identity(), identity()
        left, right = await channels()
        transport = DirectSessionTransport(signer=local, clock=RealClock(), queue_bytes=4096)
        entered, release = threading.Event(), threading.Event()
        callback_threads = []
        def slow(_, message):
            callback_threads.append(threading.get_ident())
            entered.set()
            release.wait(timeout=3)
        transport.on_message(slow)
        try:
            await transport.attach_peer(left, remote_key=remote.public_key_bytes(), conn_id="guest")
            raw = wire.pack("burst", {"data": "x" * 2500}, signer=remote, clock=RealClock())
            await right.send(raw)
            await until(entered.is_set)
            await right.send(raw)
            await until(lambda: transport.peer_ids() == [])
            assert callback_threads == [callback_threads[0]]
            assert callback_threads[0] != threading.get_ident()
            assert 0 <= transport.diagnostics()["queued_inbound_bytes"] <= 4096
        finally:
            release.set()
            await transport.aclose()
            await right.close()
        assert transport.diagnostics()["queued_inbound_bytes"] == 0
    asyncio.run(run())


def test_bounded_outgoing_burst_keeps_fifo_and_one_native_send(monkeypatch):
    async def run():
        local, remote = identity(), identity()
        left, right = await channels()
        sender = DirectSessionTransport(signer=local, clock=RealClock())
        receiver = DirectSessionTransport(signer=remote, clock=RealClock())
        received, concurrent = [], []
        in_send = 0
        native_send = left.send
        async def tracked_send(raw):
            nonlocal in_send
            in_send += 1
            concurrent.append(in_send)
            try:
                await asyncio.sleep(.001)
                await native_send(raw)
            finally:
                in_send -= 1
        monkeypatch.setattr(left, "send", tracked_send)
        receiver.on_message(lambda _cid, message: received.append(message["payload"]["i"]))
        try:
            await sender.attach_peer(left, remote_key=remote.public_key_bytes(), conn_id="guest")
            await receiver.attach_peer(right, remote_key=local.public_key_bytes(), conn_id="host")
            for i in range(64):
                sender.send("guest", {"type": "burst", "payload": {"i": i}})
            await sender.drain()
            await until(lambda: len(received) == 64)
            assert received == list(range(64))
            assert max(concurrent) == 1
            assert sender.diagnostics()["sent_messages"] == 64
            assert receiver.diagnostics()["received_messages"] == 64
        finally:
            await asyncio.gather(sender.aclose(), receiver.aclose())
    asyncio.run(run())


def test_close_during_pending_outgoing_work_leaves_no_tasks_or_worker():
    async def run():
        baseline = set(asyncio.all_tasks())
        local, remote = identity(), identity()
        left, right = await channels()
        transport = DirectSessionTransport(signer=local, clock=RealClock())
        try:
            await transport.attach_peer(left, remote_key=remote.public_key_bytes(), conn_id="guest")
            for i in range(256):
                transport.send("guest", {"type": "pending", "payload": {"i": i}})
            with pytest.raises(DirectTransportError, match="queue limit"):
                transport.send("guest", {"type": "overflow"})
            await transport.aclose()
        finally:
            await transport.aclose()
            await right.close()
        await asyncio.sleep(0)
        assert transport.diagnostics()["peers"] == 0
        assert transport.diagnostics()["owned_tasks_pending"] == 0
        assert not transport.diagnostics()["callback_worker_alive"]
        assert not [task for task in asyncio.all_tasks() - baseline if not task.done()]
    asyncio.run(run())


def test_outgoing_byte_limit_still_refuses_oversized_bursts():
    async def run():
        local, remote = identity(), identity()
        left, right = await channels()
        transport = DirectSessionTransport(signer=local, clock=RealClock(), queue_bytes=1024)
        try:
            await transport.attach_peer(left, remote_key=remote.public_key_bytes(), conn_id="guest")
            message = {"type": "burst", "payload": {"data": "x" * 400}}
            transport.send("guest", message)
            with pytest.raises(DirectTransportError, match="queue limit"):
                transport.send("guest", message)
            await until(lambda: not transport.peer_ids())
        finally:
            await transport.aclose()
            await right.close()
    asyncio.run(run())


def test_native_cleanup_error_is_not_reported_as_success(monkeypatch):
    async def run():
        local, remote = identity(), identity()
        left, right = await channels()
        transport = DirectSessionTransport(signer=local, clock=RealClock())
        await transport.attach_peer(left, remote_key=remote.public_key_bytes())
        original = left.close
        async def failure():
            await original()
            raise RuntimeError("fixture native cleanup failure")
        monkeypatch.setattr(left, "close", failure)
        try:
            with pytest.raises(DirectTransportError, match="cleanup failed"):
                await transport.aclose()
            assert not transport.diagnostics()["callback_worker_alive"]
        finally:
            await right.close()
    asyncio.run(run())
