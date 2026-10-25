"""Real admission, cryptographic cards and replica poker over direct channels."""
from __future__ import annotations

import asyncio
import secrets

import pytest

pytest.importorskip("aiortc", reason="install the optional direct Internet host stack")
from crypto_gate import require_crypto
from holdem.ice_peer import DirectPeer
from holdem.ice_room import DirectPokerRoom
from holdem.p2p.identity import Identity
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def identity():
    return Identity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())


async def wait_for(operation, predicate, label, timeout=20):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await operation()
        if predicate(value):
            return value
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(label + ": " + repr(value)[:3000])
        await asyncio.sleep(.01)


async def link(host, guest, host_key, guest_key):
    first, second = DirectPeer(stun_urls=()), DirectPeer(stun_urls=())
    try:
        await first.accept_answer(await second.accept_offer(await first.create_offer()))
        await asyncio.gather(first.wait_open(10), second.wait_open(10))
        await host.attach_peer(first, remote_key=guest_key.public_key_bytes())
        await guest.attach_peer(second, remote_key=host_key.public_key_bytes())
    except BaseException:
        await asyncio.gather(first.close(), second.close())
        raise


def room(key, host_key, secret, token, *, is_host, nickname, admission_timeout=15):
    return DirectPokerRoom(is_host=is_host, identity=key, master_secret=secrets.token_bytes(32),
                           nickname=nickname, admission_secret=secret, discovery_token=token,
                           host_public_key=host_key.public_key_bytes(), settings={"stack": 10000},
                           admission_timeout=admission_timeout)


async def start_rooms(count):
    keys = [identity() for _ in range(count)]
    secret, token = secrets.token_bytes(16), secrets.token_bytes(8)
    rooms = [room(key, keys[0], secret, token, is_host=(i == 0), nickname="Player" + str(i))
             for i, key in enumerate(keys)]
    try:
        assert not (await rooms[0].lobby())["can_start"]
        for i in range(1, count):
            await link(rooms[0], rooms[i], keys[0], keys[i])
        for current in rooms:
            await wait_for(current.lobby, lambda l: len(l["players"]) == count,
                           "all direct players admitted")
            await current.set_ready(True)
        await wait_for(rooms[0].lobby, lambda l: l["can_start"], "clock and roster agreement")
        verdict = await rooms[0].command("start_game")
        assert verdict["ok"] and verdict["verdict"] == "started"
        return rooms
    except BaseException:
        await asyncio.gather(*(r.aclose() for r in rooms))
        raise


def public_view(snapshot):
    return {name: snapshot.get(name) for name in
            ("phase", "hand_num", "street", "board", "pot", "button", "sb_seat", "bb_seat", "action_on")} | {
                "seats": [{name: seat[name] for name in ("seat", "name", "stack", "bet", "folded", "all_in")}
                          for seat in snapshot["seats"]]}


async def views(rooms):
    return await asyncio.gather(*(r.view() for r in rooms))


async def synchronized(rooms, hand):
    return await wait_for(lambda: views(rooms), lambda snapshots: all(
        s.get("hand_num") == hand and s.get("phase") in ("betting", "settled") and
        (s["phase"] != "betting" or s.get("action_on", -1) >= 0) for s in snapshots) and
        all(public_view(s) == public_view(snapshots[0]) for s in snapshots[1:]),
        "direct replicas agree")


async def play_hand(rooms, hand, *, total_chips=None):
    for _ in range(40):
        snapshots = await synchronized(rooms, hand)
        first = snapshots[0]
        if first["phase"] == "settled":
            assert len(first["board"]) == 5
            assert sum(s["stack"] for s in first["seats"]) == (total_chips or len(rooms) * 10000)
            assert first["pot"] == 0
            return [s["stack"] for s in first["seats"]]
        holes = [card for s in snapshots for card in s["you"]["hole"]]
        assert len(holes) == len(set(holes)) == 2 * len(rooms)
        for s in snapshots:
            assert s["deal_policy"] == "bayer-groth-v1"
            assert s["proofs_verified"] >= len(rooms) - 1
            assert not any("hole" in seat for seat in s["seats"])
        actor = next(r for r, s in zip(rooms, snapshots) if s["seat"] == first["action_on"])
        result = await actor.command("check_call")
        assert result["ok"] and result["verdict"] == "applied", result
    raise AssertionError("direct hand did not settle")


async def advance(rooms):
    pending = list(rooms)
    deadline = asyncio.get_running_loop().time() + 10
    while pending:
        for r in list(pending):
            verdict = (await r.command("next_hand"))["verdict"]
            assert verdict in ("started", "not_ready"), verdict
            if verdict == "started":
                pending.remove(r)
        if pending:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(.01)


@pytest.mark.parametrize("count,hands", [(2, 3), (3, 10), (9, 1)],
                         ids=["two-peers", "three-peers-ten-hands", "nine-peers-one-hand"])
def test_authenticated_rooms_play_real_private_hands_and_agree_balances(count, hands):
    require_crypto()
    async def run():
        baseline = set(asyncio.all_tasks())
        rooms = await start_rooms(count)
        results = []
        try:
            for hand in range(1, hands + 1):
                results.append(await play_hand(rooms, hand))
                if hand != hands:
                    await advance(rooms)
            assert len(results) == hands
            assert len(rooms[0].transport.peer_ids()) == count - 1
            assert all(len(r.transport.peer_ids()) == 1 for r in rooms[1:])
        finally:
            await asyncio.gather(*(r.aclose() for r in rooms))
        await asyncio.sleep(0)
        assert not [t for t in asyncio.all_tasks() - baseline if not t.done()]
        assert all(not r.transport.diagnostics()["callback_worker_alive"] for r in rooms)
        assert all(r.transport.diagnostics()["owned_tasks_pending"] == 0 for r in rooms)
    asyncio.run(run())


def test_wrong_invite_capability_never_joins_the_roster():
    require_crypto()
    async def run():
        keys = [identity(), identity()]
        token = secrets.token_bytes(8)
        host = room(keys[0], keys[0], b"a" * 16, token, is_host=True, nickname="Host", admission_timeout=.3)
        guest = room(keys[1], keys[0], b"b" * 16, token, is_host=False, nickname="Wrong secret", admission_timeout=.3)
        try:
            await link(host, guest, *keys)
            await wait_for(guest.lobby, lambda l: l["terminal_state"] is not None, "wrong capability rejected")
            lobby = await host.lobby()
            assert len(lobby["players"]) == 1 and lobby["terminal_state"] is None
            assert (await guest.lobby())["players"] == []
        finally:
            await asyncio.gather(host.aclose(), guest.aclose())
    asyncio.run(run())


def test_wrong_full_host_pin_is_rejected_before_admission():
    require_crypto()
    async def run():
        host_key, guest_key, wrong_key = identity(), identity(), identity()
        token, secret = secrets.token_bytes(8), secrets.token_bytes(16)
        host = room(host_key, host_key, secret, token, is_host=True, nickname="Host")
        guest = room(guest_key, wrong_key, secret, token, is_host=False, nickname="Guest")
        try:
            with pytest.raises(ValueError, match="full host pin"):
                await link(host, guest, host_key, guest_key)
            lobby = await host.lobby()
            assert len(lobby["players"]) == 1 and lobby["terminal_state"] is None
            assert (await guest.lobby())["players"] == []
        finally:
            await asyncio.gather(host.aclose(), guest.aclose())
    asyncio.run(run())


def test_host_loss_closes_guest_with_last_agreed_balances():
    require_crypto()
    async def run():
        rooms = await start_rooms(2)
        try:
            balances = await play_hand(rooms, 1)
            await rooms[0].aclose()
            snapshot = await wait_for(rooms[1].view, lambda s: s.get("terminal") is not None,
                                      "direct host loss visible")
            assert snapshot["terminal"]["state"] == "HOST_LOST"
            assert snapshot["terminal"]["last_settled_stacks"] == balances
            assert snapshot["turn"]["state"] == "table_closed"
            assert "legal" not in snapshot["you"]
        finally:
            await asyncio.gather(*(r.aclose() for r in rooms))
    asyncio.run(run())


def test_guest_loss_settles_and_two_survivors_continue_on_independent_channel():
    require_crypto()
    async def run():
        rooms = await start_rooms(3)
        try:
            await synchronized(rooms, 1)
            await rooms[1].aclose()
            survivors = [rooms[0], rooms[2]]
            snapshots = await wait_for(lambda: views(survivors), lambda snaps:
                all(s.get("phase") in ("void", "settled") and s.get("terminal") is None for s in snaps) and
                public_view(snaps[0]) == public_view(snaps[1]), "direct guest departure agreement")
            assert sum(s["stack"] for s in snapshots[0]["seats"]) == 30000
            assert len(rooms[0].transport.peer_ids()) == 1
            await advance(survivors)
            current = await rooms[0].view()
            assert current["hand_num"] > 1
            await play_hand(survivors, current["hand_num"], total_chips=30000)
        finally:
            await asyncio.gather(*(r.aclose() for r in rooms))
    asyncio.run(run())
