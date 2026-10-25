"""Automatic invitations compose actual independent direct poker sessions."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import secrets

import pytest

pytest.importorskip("aiortc")
pytest.importorskip("websockets")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from crypto_gate import require_crypto
from holdem.internet_invite import encode_invite, parse_invite, InviteError
from holdem.internet_session import InternetSession, configured_services
from holdem.introduction_client import validate_endpoint
from holdem.introduction_service import IntroductionService
from holdem.p2p.identity import Identity


def session(endpoint):
    return InternetSession(nickname="Player" + secrets.token_hex(2), settings={"stack": 10000},
                           services={"test": endpoint},
                           identity=Identity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate()),
                           master_secret=secrets.token_bytes(32), stun_urls=())


def test_desktop_has_a_trusted_default_without_player_configuration(monkeypatch):
    monkeypatch.delenv("HOLDEM_INTRODUCTION_URL", raising=False)
    assert configured_services() == {"poker": "wss://poker.errantsaints.space/v1"}


async def wait(operation, predicate, label, timeout=20):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = await operation()
        if predicate(result):
            return result
        if asyncio.get_running_loop().time() >= deadline:
            # Never dump snapshots containing an invite or private cards.
            raise AssertionError(label)
        await asyncio.sleep(.01)


def public(snapshot):
    return {key: snapshot.get(key) for key in
            ("phase", "hand_num", "street", "board", "pot", "button", "action_on")} | {
                "seats": [{key: seat[key] for key in ("seat", "name", "stack", "bet", "folded", "all_in")}
                          for seat in snapshot["seats"]]}


async def synchronized(sessions, hand):
    async def snapshots():
        return await asyncio.gather(*(s.snapshot() for s in sessions))
    return await wait(snapshots, lambda snaps: all(
        s.get("hand_num") == hand and s.get("phase") in ("betting", "settled") and
        (s["phase"] == "settled" or s.get("action_on", -1) >= 0) for s in snaps) and
        all(public(s) == public(snaps[0]) for s in snaps[1:]), "automatic direct replicas agree")


async def play(sessions, hand):
    for _ in range(50):
        snapshots = await synchronized(sessions, hand)
        first = snapshots[0]
        assert all(s["deal_policy"] == "bayer-groth-v1" and s["proofs_verified"] == 3 for s in snapshots)
        if first["phase"] == "settled":
            assert len(first["board"]) == 5 and first["pot"] == 0
            assert sum(s["stack"] for s in first["seats"]) == 30000
            return [s["stack"] for s in first["seats"]]
        assert all(not any("hole" in seat for seat in s["seats"]) for s in snapshots)
        actor = next(current for current, snap in zip(sessions, snapshots) if snap["seat"] == first["action_on"])
        assert (await actor.command("check_call"))["verdict"] == "applied"
    raise AssertionError("automatic direct hand did not finish")


async def advance(sessions):
    pending = set(sessions)
    deadline = asyncio.get_running_loop().time() + 10
    while pending:
        for current in list(pending):
            verdict = (await current.command("next_hand"))["verdict"]
            assert verdict in ("started", "not_ready")
            if verdict == "started":
                pending.remove(current)
        if pending:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(.01)


def test_three_players_join_from_one_invite_and_play_ten_hands_after_service_stops():
    require_crypto()
    async def run():
        baseline = set(asyncio.all_tasks())
        service = IntroductionService(service="test")
        await service.start()
        sessions = [session(service.endpoint) for _ in range(3)]
        try:
            await sessions[0].host()
            snap = await wait(sessions[0].snapshot, lambda s: bool(s["lobby"]["invite"]), "host invite created")
            code = snap["lobby"]["invite"]
            for guest in sessions[1:]:
                await guest.join(code)
            for current in sessions:
                await wait(current.snapshot, lambda s: len(s["lobby"]["players"]) == 3 and
                           s["lobby"]["state"] == "lobby", "all invited players admitted")
                await current.set_ready(True)
            await wait(sessions[0].snapshot, lambda s: s["lobby"]["can_start"], "all clocks and ready agree")
            # The setup service disappears before any gameplay starts.
            await service.aclose()
            assert await sessions[0].start() == "started"
            balances = []
            for hand in range(1, 11):
                balances.append(await play(sessions, hand))
                if hand != 10:
                    await advance(sessions)
            assert len(balances) == 10
            final = await asyncio.gather(*(current.snapshot() for current in sessions))
            assert all(s["lobby"]["state"] == "playing" for s in final)
        finally:
            await asyncio.gather(*(s.aclose() for s in sessions))
            await service.aclose()
        await asyncio.sleep(.01)
        assert not [t for t in asyncio.all_tasks() - baseline if not t.done()]
        assert all(not s._room.transport.diagnostics()["callback_worker_alive"] for s in sessions)
    asyncio.run(run())


def test_wrong_private_capability_is_rejected_before_host_creates_a_peer():
    require_crypto()
    async def run():
        service = IntroductionService(service="test")
        await service.start()
        host, guest = session(service.endpoint), session(service.endpoint)
        try:
            await host.host()
            snap = await wait(host.snapshot, lambda s: bool(s["lobby"]["invite"]), "host invite")
            invite = parse_invite(snap["lobby"]["invite"], services={"test": service.endpoint})
            wrong = encode_invite(replace(invite, admission_secret="a" * 32))
            await guest.join(wrong)
            await wait(guest.snapshot, lambda s: s["lobby"]["state"] == "closed", "wrong capability rejected")
            assert len((await host.snapshot())["lobby"]["players"]) == 1
            assert host._room.transport.peer_ids() == []
            assert guest._room is None
        finally:
            await asyncio.gather(host.aclose(), guest.aclose())
            await service.aclose()
    asyncio.run(run())


def test_invalid_invite_preserves_the_current_table_and_pending_leave_cancels_setup(monkeypatch):
    require_crypto()
    async def run():
        service = IntroductionService(service="test")
        await service.start()
        host = session(service.endpoint)
        try:
            await host.host()
            await wait(host.snapshot, lambda s: bool(s["lobby"]["invite"]), "host invite")
            original = host._room
            with pytest.raises(InviteError):
                await host.join("unrecognized-invite")
            assert host._room is original
            assert (await host.snapshot())["lobby"]["state"] == "lobby"
        finally:
            await host.aclose()
            await service.aclose()
        entered = asyncio.Event()
        async def never(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr("holdem.internet_session.IntroductionConnection.connect", never)
        pending = session("ws://127.0.0.1:1/v1")
        await pending.host()
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(pending.aclose(), 5)
        assert pending._main_task.done()
        assert not pending._room.transport.diagnostics()["callback_worker_alive"]
    asyncio.run(run())


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:1/v1", "ws://example.com/v1", "wss://u:p@example.com/v1",
                                      "wss://example.com/v1?invite=private", "wss://example.com/other"])
def test_service_destinations_require_configured_secure_origins(endpoint):
    with pytest.raises(ValueError):
        validate_endpoint(endpoint, allow_loopback=True)


def test_ready_change_during_start_keeps_the_introduction_and_valid_invite_alive():
    async def run():
        class Room:
            async def lobby(self):
                return {"can_start": True}
            async def command(self, _command):
                return {"ok": False, "verdict": "not_ready"}
            async def aclose(self):
                pass
        current = session("ws://127.0.0.1:1/v1")
        current._room = Room()
        current._mode, current._state, current._accepting = "host", "lobby", True
        current._invite = "private-invite-fixture"
        current._main_task = asyncio.create_task(asyncio.Event().wait())
        try:
            assert await current.start() == "not_ready"
            assert current._accepting and not current._starting
            assert not current._main_task.done()
            assert current._invite == "private-invite-fixture"
        finally:
            await current.aclose()
    asyncio.run(run())
