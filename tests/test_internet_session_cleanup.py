"""Owned setup cleanup failures stay visible without skipping other closes."""
from __future__ import annotations

import asyncio
import secrets
import time

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from holdem import ice_setup
from holdem.internet_invite import InternetInvite
from holdem.internet_session import InternetSession
from holdem.p2p.identity import Identity


PRIVATE = "private native address and credentials must never escape"


class Resource:
    def __init__(self, *, failures=0):
        self.calls = 0
        self.failures = failures
        self.closed = False

    async def aclose(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError(PRIVATE)
        self.closed = True


class Peer(Resource):
    async def create_offer(self):
        raise ConnectionError("normal failed direct attempt")

    async def accept_offer(self, _description):
        raise ConnectionError("normal failed direct attempt")

    async def close(self):
        await self.aclose()


class Intro(Resource):
    challenge = "c" * 64

    def __init__(self, replies=(), *, failures=0):
        super().__init__(failures=failures)
        self.replies = iter(replies)
        self.sent = []

    async def send(self, message):
        self.sent.append(message)

    async def receive(self, _timeout):
        try:
            return next(self.replies)
        except StopIteration:
            raise ConnectionError("normal service failure") from None


def identity():
    return Identity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())


def session():
    return InternetSession(nickname="Player", settings={}, services={"test": "ws://127.0.0.1:1/v1"},
                           identity=identity(), master_secret=secrets.token_bytes(32), stun_urls=())


async def assert_failed_close(current):
    with pytest.raises(RuntimeError, match="^Internet connection cleanup did not complete$") as caught:
        await current.aclose()
    assert PRIVATE not in str(caught.value)
    assert current._cleanup_failed and current._state == "closed"
    # A second call must not turn a previously failed close into success.
    with pytest.raises(RuntimeError, match="^Internet connection cleanup did not complete$"):
        await current.aclose()


def test_unattached_host_peer_failure_is_sticky_and_retried_before_room_close(monkeypatch):
    async def run():
        current = session()
        peer = Peer(failures=1)
        intro, room = Intro(), Resource()
        current._accepting = True
        current._intro, current._room = intro, room
        monkeypatch.setattr(current, "_peer", lambda: peer)
        guest = {"guest_public": "a" * 64}
        await current._connect_guest(intro, guest, {"expires_at": int(time.time()) + 90}, 0)
        assert current._cleanup_failed
        assert current._failed_peers == [peer]
        await assert_failed_close(current)
        assert peer.closed and peer.calls == 2
        assert room.closed and intro.closed and not current._failed_peers
    asyncio.run(run())


def test_escaped_pending_failure_remains_visible_after_callback_removes_task():
    async def run():
        current = session()
        intro, room = Intro(), Resource()
        current._intro, current._room = intro, room
        async def fail():
            raise RuntimeError(PRIVATE)
        task = asyncio.create_task(fail())
        current._pending["guest"] = {"task": task}
        await asyncio.sleep(0)
        current._guest_done("guest", task)
        assert not current._pending and current._cleanup_failed
        await assert_failed_close(current)
        assert room.closed and intro.closed
    asyncio.run(run())


def test_escaped_main_failure_is_observed_and_all_other_resources_close():
    async def run():
        current = session()
        intro, room = Intro(failures=10), Resource()
        current._intro, current._room = intro, room
        async def fail():
            raise RuntimeError(PRIVATE)
        current._main_task = asyncio.create_task(fail())
        await asyncio.sleep(0)
        await assert_failed_close(current)
        assert room.closed and intro.calls == 1
    asyncio.run(run())


def test_host_intro_close_failure_still_cancels_renew_pending_and_closes_room(monkeypatch):
    async def run():
        current = session()
        current._mode, current._state, current._accepting = "host", "connecting", True
        intro, room = Intro(failures=10), Resource()
        current._intro = intro
        async def prepare(**_kwargs):
            current._room = room
        async def connection(_service):
            return intro
        monkeypatch.setattr(current, "_prepare", prepare)
        monkeypatch.setattr(current, "_connection", connection)
        current._renew_task = asyncio.create_task(asyncio.Event().wait())
        pending = asyncio.create_task(asyncio.Event().wait())
        answer = asyncio.get_running_loop().create_future()
        current._pending["guest"] = {"task": pending, "answer": answer}
        current._main_task = asyncio.create_task(current._host(0))
        await current._main_task
        assert current._renew_task.cancelled() and pending.cancelled() and answer.cancelled()
        assert not current._pending and room.closed and current._room is None
        await assert_failed_close(current)
        assert intro.calls == 2
    asyncio.run(run())


def test_joiner_intro_and_unattached_peer_failures_still_close_its_room(monkeypatch):
    async def run():
        current = session()
        current._mode, current._state = "join", "connecting"
        host = identity()
        now = int(time.time())
        record = ice_setup.sign_record({
            "v": 1, "purpose": ice_setup.HOST_PURPOSE, "service": "test",
            "host_public": host.public_key_bytes().hex(), "setup_public": host.x25519_public_key_bytes().hex(),
            "discovery_token": "a" * 16, "generation": "b" * 32, "challenge": "c" * 64,
            "created_at": now, "expires_at": now + 600,
        }, host)
        invite = InternetInvite(service="test", host_public=record["host_public"], discovery_token="a" * 16,
                                generation="b" * 32, expires_at=record["expires_at"], admission_secret="d" * 32)
        intro = Intro([{"type": "room", "record": record}, {"type": "joined"},
                       {"type": "setup", "guest_public": current._identity.public_key_bytes().hex(), "envelope": {}}],
                      failures=10)
        peer, room = Peer(failures=1), Resource()
        current._intro = intro
        async def prepare(**_kwargs):
            current._room = room
        async def connection(_service):
            return intro
        monkeypatch.setattr(current, "_prepare", prepare)
        monkeypatch.setattr(current, "_connection", connection)
        monkeypatch.setattr(current, "_peer", lambda: peer)
        monkeypatch.setattr(ice_setup, "open_description", lambda *_args, **_kwargs: {"type": "offer", "sdp": "fixture"})
        current._main_task = asyncio.create_task(current._join(invite, 0))
        await current._main_task
        assert current._cleanup_failed and peer.calls == 1
        assert room.closed and current._room is None
        await assert_failed_close(current)
        assert peer.closed and peer.calls == 2
    asyncio.run(run())


def test_ordinary_denied_join_does_not_poison_close(monkeypatch):
    async def run():
        current = session()
        current._mode, current._state = "join", "connecting"
        intro, room = Intro(), Resource()
        current._intro = intro
        invite = InternetInvite(service="test", host_public="a" * 64, discovery_token="b" * 16,
                                generation="c" * 32, expires_at=int(time.time()) + 600, admission_secret="d" * 32)
        async def prepare(**_kwargs):
            current._room = room
        async def connection(_service):
            return intro
        monkeypatch.setattr(current, "_prepare", prepare)
        monkeypatch.setattr(current, "_connection", connection)
        current._main_task = asyncio.create_task(current._join(invite, 0))
        await current._main_task
        assert current._state == "closed" and not current._cleanup_failed
        await current.aclose()
        assert intro.closed and room.closed
    asyncio.run(run())


def test_room_close_failure_does_not_hide_closed_state_or_other_closes():
    async def run():
        current = session()
        intro, room = Intro(), Resource(failures=10)
        current._intro, current._room = intro, room
        await assert_failed_close(current)
        assert intro.closed and room.calls == 1
    asyncio.run(run())
