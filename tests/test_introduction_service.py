"""Real setup sockets enforce public authentication and bounded ownership."""
from __future__ import annotations

import asyncio
import json
import secrets
import time

import pytest

pytest.importorskip("websockets")
pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidMessage, InvalidStatus

from holdem import ice_setup as S
from holdem.introduction_service import IntroductionService
from holdem.p2p.identity import Identity


def identity():
    return Identity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())


async def receive(socket):
    raw = await asyncio.wait_for(socket.recv(), 3)
    return S.load_json(raw.encode() if type(raw) is str else raw)


async def send(socket, message):
    await socket.send(S.canonical(message).decode())


async def eventually(predicate):
    deadline = asyncio.get_running_loop().time() + 4
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "service condition did not settle"
        await asyncio.sleep(.01)


async def opening(service):
    socket = await connect(service.endpoint, compression=None, proxy=None, close_timeout=1)
    challenge = await receive(socket)
    assert set(challenge) == {"type", "service", "challenge", "expires_at"}
    assert challenge["type"] == "challenge" and challenge["service"] == service.service
    return socket, challenge


def host_record(key, service, challenge, *, now=None, expiry=600):
    now = int(time.time()) if now is None else now
    return S.sign_record({"v": 1, "purpose": S.HOST_PURPOSE, "service": service,
                          "host_public": key.public_key_bytes().hex(),
                          "discovery_token": secrets.token_hex(8), "generation": secrets.token_hex(16),
                          "setup_public": key.x25519_public_key_bytes().hex(), "challenge": challenge,
                          "created_at": now, "expires_at": now + expiry}, key)


async def host(service, *, now=None, expiry=600):
    key = identity()
    socket, challenge = await opening(service)
    record = host_record(key, service.service, challenge["challenge"], now=now, expiry=expiry)
    await send(socket, {"type": "host_register", "record": record})
    assert await receive(socket) == {"type": "registered", "record": record}
    return key, socket, record


async def guest(service, host_socket, descriptor, *, now=None, expiry=60):
    key = identity()
    socket, challenge = await opening(service)
    await send(socket, {"type": "lookup", **{name: descriptor[name] for name in
                                            ("host_public", "discovery_token", "generation")}})
    assert await receive(socket) == {"type": "room", "record": descriptor}
    now = int(time.time()) if now is None else now
    record = S.sign_record({"v": 1, "purpose": S.GUEST_PURPOSE, "service": service.service,
                           **{name: descriptor[name] for name in ("host_public", "discovery_token", "generation")},
                           "guest_public": key.public_key_bytes().hex(),
                           "setup_public": key.x25519_public_key_bytes().hex(),
                           "guest_nonce": secrets.token_hex(32), "challenge": challenge["challenge"],
                           "created_at": now, "expires_at": now + expiry,
                           # The server cannot verify possession of the private
                           # invitation. Only the host checks this proof.
                           "capability_proof": "0" * 64}, key)
    await send(socket, {"type": "guest_join", "record": record})
    assert await receive(socket) == {"type": "joined"}
    assert await receive(host_socket) == {"type": "guest", "record": record}
    return key, socket, record


def test_two_guests_exchange_encrypted_setup_and_host_closes_room():
    async def run():
        baseline = set(asyncio.all_tasks())
        service = IntroductionService("test")
        async with service:
            hkey, hs, descriptor = await host(service)
            first = await guest(service, hs, descriptor)
            second = await guest(service, hs, descriptor)
            try:
                for key, socket, record in (first, second):
                    context = S.setup_context(descriptor, record)
                    for sender_key, sender_socket, recipient, recipient_socket, kind in (
                            (hkey, hs, key, socket, "offer"), (key, socket, hkey, hs, "answer")):
                        envelope = S.seal_description({"type": kind, "sdp": "private setup description"},
                                                      context, sender_key, recipient.x25519_public_key_bytes().hex())
                        frame = {"type": "setup", "guest_public": record["guest_public"], "envelope": envelope}
                        await send(sender_socket, frame)
                        assert await receive(recipient_socket) == frame
                        assert "private setup description" not in json.dumps(frame)
                assert service.counts()["rooms"] == 1 and service.counts()["pending_guests"] == 2
                await send(hs, {"type": "renew"})
                assert await receive(hs) == {"type": "renewed"}
                await send(hs, {"type": "close"})
                for socket in (hs, first[1], second[1]):
                    with pytest.raises(ConnectionClosed):
                        await receive(socket)
                await eventually(lambda: service.counts()["retained_bytes"] == 0)
            finally:
                await asyncio.gather(hs.close(), first[1].close(), second[1].close())
        assert all(value == 0 for value in service.counts().values())
        await asyncio.sleep(0)
        assert not [task for task in asyncio.all_tasks() - baseline if not task.done()]
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["challenge", "signature", "secret-field", "cross-service", "expired"])
def test_invalid_host_registration_never_reserves_a_room(failure):
    async def run():
        async with IntroductionService("test") as service:
            key = identity()
            socket, challenge = await opening(service)
            record = host_record(key, "test", challenge["challenge"])
            if failure == "signature":
                record["signature"] = "0" * 128
            else:
                if failure == "challenge":
                    record["challenge"] = "0" * 64
                elif failure == "secret-field":
                    record["admission_secret"] = "1" * 32
                elif failure == "cross-service":
                    record["service"] = "other"
                else:
                    record["expires_at"] = record["created_at"] - 1
                record = S.sign_record(record, key)
            await send(socket, {"type": "host_register", "record": record})
            with pytest.raises(ConnectionClosed):
                await receive(socket)
            await eventually(lambda: service.counts()["pending_challenges"] == 0)
            assert service.counts()["rooms"] == service.counts()["retained_bytes"] == 0
    asyncio.run(run())


def test_replayed_registration_cannot_consume_a_new_socket_challenge():
    async def run():
        async with IntroductionService("test") as service:
            _, first, record = await host(service)
            second, challenge = await opening(service)
            assert challenge["challenge"] != record["challenge"]
            await send(second, {"type": "host_register", "record": record})
            with pytest.raises(ConnectionClosed):
                await receive(second)
            assert service.counts()["rooms"] == 1
            await first.close()
    asyncio.run(run())


def test_guest_departure_and_host_rejection_leave_other_guests_pending():
    async def run():
        async with IntroductionService("test") as service:
            _, hs, descriptor = await host(service)
            one = await guest(service, hs, descriptor)
            two = await guest(service, hs, descriptor)
            await one[1].close()
            await eventually(lambda: service.counts()["pending_guests"] == 1)
            assert service.counts()["rooms"] == 1
            await send(hs, {"type": "reject", "guest_public": two[2]["guest_public"]})
            with pytest.raises(ConnectionClosed):
                await receive(two[1])
            await eventually(lambda: service.counts()["pending_guests"] == 0)
            await send(hs, {"type": "renew"})
            assert await receive(hs) == {"type": "renewed"}
            await hs.close()
    asyncio.run(run())


def test_late_rejection_after_guest_departure_keeps_room_open_for_other_friends():
    async def run():
        async with IntroductionService("test") as service:
            _, hs, descriptor = await host(service)
            _, gs, departed = await guest(service, hs, descriptor)
            await gs.close()
            await eventually(lambda: service.counts()["pending_guests"] == 0)
            # A host's pending ICE attempt may time out after the guest left.
            await send(hs, {"type": "reject", "guest_public": departed["guest_public"]})
            await send(hs, {"type": "reject", "guest_public": departed["guest_public"]})
            await send(hs, {"type": "renew"})
            assert await receive(hs) == {"type": "renewed"}
            assert service.counts()["rooms"] == 1
            _, later, _ = await guest(service, hs, descriptor)
            assert service.counts()["pending_guests"] == 1
            await asyncio.gather(hs.close(), later.close())
    asyncio.run(run())


@pytest.mark.parametrize("public", [None, [], "a" * 63, "A" * 64, "g" * 64])
def test_idempotent_reject_still_refuses_malformed_guest_keys(public):
    async def run():
        async with IntroductionService("test") as service:
            _, hs, _ = await host(service)
            await send(hs, {"type": "reject", "guest_public": public})
            with pytest.raises(ConnectionClosed):
                await receive(hs)
            await eventually(lambda: service.counts()["rooms"] == 0)
    asyncio.run(run())


@pytest.mark.parametrize("selected", ["x-forwarded-for", "cf-connecting-ip"])
def test_only_selected_header_from_exact_trusted_proxy_supplies_client_ip(selected):
    async def run():
        other = "cf-connecting-ip" if selected == "x-forwarded-for" else "x-forwarded-for"
        async with IntroductionService("test", trusted_proxies=("127.0.0.1",), proxy_header=selected) as service:
            socket = await connect(service.endpoint, proxy=None,
                                   additional_headers=[(selected, "203.0.113.9"), (other, "spoofed, chain")])
            await receive(socket)
            assert len(service._peers) == 1
            assert next(iter(service._peers.values())).ip == "203.0.113.9"
            assert ("connect", "203.0.113.9") in service._rates
            await socket.close()
    asyncio.run(run())


@pytest.mark.parametrize("selected", ["x-forwarded-for", "cf-connecting-ip"])
def test_untrusted_socket_cannot_spoof_client_ip_with_either_header(selected):
    async def run():
        async with IntroductionService("test", trusted_proxies=("127.0.0.2",), proxy_header=selected) as service:
            socket = await connect(service.endpoint, proxy=None,
                                   additional_headers=[("X-Forwarded-For", "203.0.113.9"),
                                                       ("CF-Connecting-IP", "198.51.100.4")])
            await receive(socket)
            assert next(iter(service._peers.values())).ip == "127.0.0.1"
            assert ("connect", "127.0.0.1") in service._rates
            assert not any(key[1] in ("203.0.113.9", "198.51.100.4") for key in service._rates)
            await socket.close()
    asyncio.run(run())


@pytest.mark.parametrize("selected", ["x-forwarded-for", "cf-connecting-ip"])
@pytest.mark.parametrize("attack", ["missing", "duplicate", "list", "invalid", "other-only"])
def test_trusted_proxy_missing_ambiguous_or_wrong_header_fails_closed(selected, attack):
    async def run():
        other = "cf-connecting-ip" if selected == "x-forwarded-for" else "x-forwarded-for"
        headers = {
            "missing": [], "duplicate": [(selected, "203.0.113.9"), (selected, "198.51.100.4")],
            "list": [(selected, "203.0.113.9, 198.51.100.4")],
            "invalid": [(selected, "not-an-ip")], "other-only": [(other, "203.0.113.9")],
        }[attack]
        async with IntroductionService("test", trusted_proxies=("127.0.0.1",), proxy_header=selected) as service:
            with pytest.raises(InvalidStatus) as caught:
                await connect(service.endpoint, proxy=None, additional_headers=headers)
            assert caught.value.response.status_code == 429
            assert service.counts()["pending_challenges"] == 0
            assert not service._peers
    asyncio.run(run())


def test_forwarding_mode_and_proxy_addresses_are_explicit():
    with pytest.raises(ValueError, match="trusted proxy header"):
        IntroductionService("test", proxy_header="some-client-header")
    with pytest.raises(ValueError):
        IntroductionService("test", trusted_proxies=("127.0.0.0/8",))


@pytest.mark.parametrize("attack", ["duplicate", "wrong-guest", "wrong-generation"])
def test_setup_replay_and_misrouting_are_refused(attack):
    async def run():
        async with IntroductionService("test") as service:
            key, hs, descriptor = await host(service)
            gkey, gs, record = await guest(service, hs, descriptor)
            context = S.setup_context(descriptor, record)
            envelope = S.seal_description({"type": "offer", "sdp": "v=0"}, context, key, record["setup_public"])
            frame = {"type": "setup", "guest_public": record["guest_public"], "envelope": envelope}
            if attack == "duplicate":
                await send(hs, frame)
                assert await receive(gs) == frame
            elif attack == "wrong-guest":
                frame["guest_public"] = "f" * 64
            else:
                envelope["context"]["generation"] = "f" * 32
            await send(hs, frame)
            with pytest.raises(ConnectionClosed):
                await receive(hs)
            with pytest.raises(ConnectionClosed):
                await receive(gs)
            await eventually(lambda: service.counts()["rooms"] == 0)
    asyncio.run(run())


def test_guest_cannot_close_room_or_forward_for_another_guest():
    async def run():
        async with IntroductionService("test") as service:
            _, hs, descriptor = await host(service)
            _, gs, _ = await guest(service, hs, descriptor)
            await send(gs, {"type": "close"})
            with pytest.raises(ConnectionClosed):
                await receive(gs)
            assert service.counts()["rooms"] == 1
            await send(hs, {"type": "renew"})
            assert await receive(hs) == {"type": "renewed"}
            await hs.close()
    asyncio.run(run())


@pytest.mark.parametrize("raw", ['{"type":"lookup","type":"close"}', '{"type":NaN}',
                                  '{"type":' + '[' * 10 + '0' + ']' * 10 + '}',
                                  '{"type":"lookup","extra":[' + ','.join('0' for _ in range(257)) + ']}'])
def test_strict_json_rejects_duplicate_nonfinite_deep_and_oversized_member_trees(raw):
    async def run():
        async with IntroductionService("test") as service:
            socket, _ = await opening(service)
            await socket.send(raw)
            with pytest.raises(ConnectionClosed):
                await receive(socket)
            assert service.counts()["rooms"] == 0
    asyncio.run(run())


def test_room_challenge_socket_and_global_memory_quotas():
    async def run():
        async with IntroductionService("test", limits={"max_rooms": 1}) as service:
            _, hs, _ = await host(service)
            second, challenge = await opening(service)
            record = host_record(identity(), "test", challenge["challenge"])
            await send(second, {"type": "host_register", "record": record})
            with pytest.raises(ConnectionClosed):
                await receive(second)
            assert service.counts()["rooms"] == 1
            await hs.close()
        async with IntroductionService("test", limits={"max_challenges": 1}) as service:
            first, _ = await opening(service)
            second = await connect(service.endpoint, proxy=None)
            with pytest.raises(ConnectionClosed):
                await receive(second)
            assert service.counts()["pending_challenges"] == 1
            await first.close()
        async with IntroductionService("test", limits={"max_sockets": 1}) as service:
            first, _ = await opening(service)
            with pytest.raises((InvalidMessage, ConnectionClosed, OSError)):
                await connect(service.endpoint, proxy=None)
            assert service.counts()["sockets"] == 1
            await first.close()
        async with IntroductionService("test", limits={"max_retained_bytes": 500}) as service:
            socket, challenge = await opening(service)
            record = host_record(identity(), "test", challenge["challenge"])
            await send(socket, {"type": "host_register", "record": record})
            with pytest.raises(ConnectionClosed):
                await receive(socket)
            await eventually(lambda: service.counts()["retained_bytes"] == 0)
            assert service.counts()["rooms"] == 0
    asyncio.run(run())


def test_pending_guest_limit_does_not_close_existing_room():
    async def run():
        async with IntroductionService("test", limits={"max_guests": 1}) as service:
            _, hs, descriptor = await host(service)
            one = await guest(service, hs, descriptor)
            gs, challenge = await opening(service)
            await send(gs, {"type": "lookup", **{name: descriptor[name] for name in
                                                ("host_public", "discovery_token", "generation")}})
            assert (await receive(gs))["type"] == "room"
            key = identity()
            record = S.sign_record({**one[2], "guest_public": key.public_key_bytes().hex(),
                                    "setup_public": key.x25519_public_key_bytes().hex(),
                                    "guest_nonce": secrets.token_hex(32), "challenge": challenge["challenge"]}, key)
            await send(gs, {"type": "guest_join", "record": record})
            with pytest.raises(ConnectionClosed):
                await receive(gs)
            assert service.counts()["rooms"] == service.counts()["pending_guests"] == 1
            await asyncio.gather(hs.close(), one[1].close())
    asyncio.run(run())


def test_expiry_for_challenges_guest_attempts_leases_and_absolute_room_lifetime():
    async def run():
        clock = [int(time.time())]
        async with IntroductionService("test", clock=lambda: clock[0], maintenance_interval=.01) as service:
            socket, _ = await opening(service)
            clock[0] += 31
            with pytest.raises(ConnectionClosed):
                await receive(socket)
            _, hs, descriptor = await host(service, now=clock[0])
            _, gs, _ = await guest(service, hs, descriptor, now=clock[0], expiry=10)
            clock[0] += 11
            with pytest.raises(ConnectionClosed):
                await receive(gs)
            assert service.counts()["rooms"] == 1
            clock[0] += 50
            with pytest.raises(ConnectionClosed):
                await receive(hs)
            assert service.counts()["rooms"] == 0
            _, hs, _ = await host(service, now=clock[0], expiry=90)
            clock[0] += 50
            await send(hs, {"type": "renew"})
            assert await receive(hs) == {"type": "renewed"}
            clock[0] += 41
            with pytest.raises(ConnectionClosed):
                await receive(hs)
            await eventually(lambda: service.counts()["retained_bytes"] == 0)
    asyncio.run(run())


def test_slow_outgoing_socket_hits_bounded_queue_and_releases_bytes(monkeypatch):
    async def run():
        async with IntroductionService("test", limits={"max_queued_messages": 3}) as service:
            _, hs, descriptor = await host(service)
            lookup, _ = await opening(service)
            await eventually(lambda: len(service._peers) == 2)
            server_peer = next(peer for peer in service._peers.values() if peer.role is None)
            entered = asyncio.Event()
            async def blocked_send(_message):
                entered.set()
                await asyncio.Future()
            monkeypatch.setattr(server_peer.socket, "send", blocked_send)
            query = {"type": "lookup", **{name: descriptor[name] for name in
                                          ("host_public", "discovery_token", "generation")}}
            await send(lookup, query)
            await entered.wait()
            for _ in range(4):
                await send(lookup, query)
            with pytest.raises(ConnectionClosed):
                await receive(lookup)
            await eventually(lambda: server_peer.queued_messages == server_peer.queued_bytes == 0)
            assert service.counts()["rooms"] == 1
            await hs.close()
    asyncio.run(run())


def test_ip_and_route_rate_state_is_bounded_and_expires():
    async def run():
        clock = [int(time.time())]
        async with IntroductionService("test", clock=lambda: clock[0],
                                       limits={"max_rate_buckets": 2}, maintenance_interval=.01) as service:
            socket, _ = await opening(service)
            # connect + message buckets exhaust the configured bounded state.
            await socket.send('{"type":"unknown"}')
            with pytest.raises(ConnectionClosed):
                await receive(socket)
            assert service.counts()["rate_buckets"] == 2
            clock[0] += 301
            await eventually(lambda: service.counts()["rate_buckets"] == 0)
    asyncio.run(run())
