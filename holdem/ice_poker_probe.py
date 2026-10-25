"""Packaged acceptance driver over the real peer-verified poker room.

Each process acts only for its own seat. Public receipts contain settled chip
balances and proof counts; private cards and protocol transcripts stay private.
This is engineering tooling, not a second poker rules implementation.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import secrets
import sys

from .ice_signaling import canonical, validate_context


def initialize_crypto():
    from . import native_crypto
    if getattr(sys, "frozen", False):
        # Fail closed: a portable acceptance cannot borrow a developer's DLL.
        return native_crypto.initialize(library_path=Path(sys._MEIPASS) / "native" / "libsodium.dll")
    return native_crypto.initialize()


def capability(context, keys, remote_encryption_public):
    """Derive a private two-seat engineering invite from pinned setup keys."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    validate_context(context)
    shared = keys.encryption.exchange(X25519PublicKey.from_public_bytes(
        bytes.fromhex(remote_encryption_public)))
    raw = HKDF(algorithm=hashes.SHA256(), length=24,
               salt=hashlib.sha256(canonical(context)).digest(),
               info=b"holdem-direct-poker-proof-invite-v1").derive(shared)
    return raw[:16], raw[16:]


async def exchange(peer, role, context, keys, remote_signing_public,
                   remote_encryption_public, hands=10):
    from .ice_room import DirectPokerRoom
    from .p2p.identity import Identity

    if type(hands) is not int or not 1 <= hands <= 10 or role not in ("host", "guest"):
        raise ValueError("invalid poker acceptance request")
    initialize_crypto()
    secret, token = capability(context, keys, remote_encryption_public)
    room = DirectPokerRoom(is_host=role == "host", identity=Identity(keys.signing, keys.encryption),
                           master_secret=secrets.token_bytes(32), nickname=role.title(),
                           admission_secret=secret, discovery_token=token,
                           host_public_key=bytes.fromhex(context["host_signing_public"]),
                           settings={"stack": 10000})
    loop = asyncio.get_running_loop()
    remote_complete, remote_ack = loop.create_future(), loop.create_future()
    nonce = context["nonce"]

    def receipt(_cid, message):
        if message.get("pubkey") != remote_signing_public:
            return
        kind, payload = message.get("type"), message.get("payload", {})
        if payload.get("nonce") != nonce:
            return
        target = (remote_complete if kind == "probe_poker_complete" else
                  remote_ack if kind == "probe_poker_ack" else None)
        if target is not None:
            def deliver():
                if not target.done():
                    target.set_result(payload)
            loop.call_soon_threadsafe(deliver)

    room.transport.on_message(receipt)
    balances = []
    minimum_proofs = None
    result = None
    try:
        cid = await room.attach_peer(peer, remote_key=bytes.fromhex(remote_signing_public))
        while True:
            lobby = await room.lobby()
            if lobby["terminal_state"] is not None:
                raise RuntimeError("poker admission terminated")
            if len(lobby["players"]) == 2:
                break
            await asyncio.sleep(.02)
        await room.set_ready(True)
        if role == "host":
            while not (await room.lobby())["can_start"]:
                await asyncio.sleep(.02)
            if not (await room.command("start_game"))["ok"]:
                raise RuntimeError("poker could not start")
        next_requested = False
        while len(balances) < hands:
            snapshot = await room.view()
            if snapshot.get("terminal") is not None:
                raise RuntimeError("poker session terminated")
            phase, hand = snapshot.get("phase"), snapshot.get("hand_num")
            playable = phase == "settled" or (phase == "betting" and snapshot.get("action_on", -1) >= 0)
            if playable:
                if snapshot.get("deal_policy") != "bayer-groth-v1":
                    raise RuntimeError("poker proof policy differs")
                proofs = snapshot.get("proofs_verified", 0)
                if proofs != 2 or (phase == "betting" and any("hole" in seat for seat in snapshot["seats"])):
                    raise RuntimeError("poker private-view/proof invariant failed")
                minimum_proofs = proofs if minimum_proofs is None else min(minimum_proofs, proofs)
            if hand == len(balances) + 1:
                next_requested = False
                if phase == "settled":
                    vector = [seat["stack"] for seat in snapshot["seats"]]
                    if (len(vector) != 2 or sum(vector) != 20000 or snapshot["pot"] != 0 or
                            len(snapshot["board"]) != 5):
                        raise RuntimeError("poker did not conserve/settle the hand")
                    balances.append(vector)
                elif playable and phase == "betting" and snapshot.get("action_on") == snapshot.get("seat"):
                    verdict = await room.command("check_call")
                    if not verdict["ok"] or verdict["verdict"] != "applied":
                        raise RuntimeError("poker own-seat action failed")
            elif hand == len(balances) and phase == "settled" and not next_requested:
                verdict = (await room.command("next_hand"))["verdict"]
                if verdict not in ("started", "not_ready"):
                    raise RuntimeError("poker could not request another hand")
                next_requested = verdict == "started"
            await asyncio.sleep(.02)

        public = {"hands": hands, "balances": balances, "proofs_verified": minimum_proofs,
                  "deal_policy": "bayer-groth-v1"}
        room.transport.send(cid, {"type": "probe_poker_complete", "payload": {"nonce": nonce, **public}})
        other = await asyncio.wait_for(remote_complete, 20)
        if other != {"nonce": nonce, **public}:
            raise RuntimeError("poker public receipts differ")
        digest = hashlib.sha256(canonical(public)).hexdigest()
        room.transport.send(cid, {"type": "probe_poker_ack", "payload": {"nonce": nonce, "digest": digest}})
        if await asyncio.wait_for(remote_ack, 20) != {"nonce": nonce, "digest": digest}:
            raise RuntimeError("poker completion was not acknowledged")
        await room.transport.drain()
        await asyncio.sleep(.5)
        result = {**public, "matching_balances": True}
    finally:
        await room.aclose()
    diagnostics = room.transport.diagnostics()
    if (diagnostics["last_error"] or diagnostics["callback_worker_alive"] or
            diagnostics["owned_tasks_pending"] or diagnostics["peers"]):
        raise RuntimeError("poker resources did not close")
    return result, diagnostics


async def _self_test(hands=10):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from .ice_peer import DirectPeer
    from .ice_signaling import ProbeKeys
    baseline = set(asyncio.all_tasks())
    keys = [ProbeKeys(Ed25519PrivateKey.generate(), X25519PrivateKey.generate()) for _ in range(2)]
    context = {"repository": "local/acceptance", "run_id": "1", "run_attempt": "1",
               "nonce": secrets.token_hex(32), "source_commit": "a" * 40,
               "package_sha256": "b" * 64, "host_signing_public": keys[0].signing_public,
               "host_encryption_public": keys[0].encryption_public}
    peers = [DirectPeer(stun_urls=()), DirectPeer(stun_urls=())]
    try:
        await peers[0].accept_answer(await peers[1].accept_offer(await peers[0].create_offer()))
        await asyncio.gather(*(p.wait_open(10) for p in peers))
        results = await asyncio.wait_for(asyncio.gather(*(
            exchange(peers[i], "guest" if i else "host", context, keys[i], keys[1-i].signing_public,
                     keys[1-i].encryption_public, hands) for i in range(2))), 120)
        if results[0][0] != results[1][0]:
            raise RuntimeError("local poker receipts differ")
    finally:
        await asyncio.gather(*(p.close() for p in peers))
    await asyncio.sleep(0)
    pending = sum(not t.done() for t in asyncio.all_tasks() - baseline)
    if pending:
        raise RuntimeError("local poker tasks remained")
    return {"schema": "direct-poker-self-test-v1", "status": "passed", "hands": hands,
            "matching_balances": True, "proofs_verified": results[0][0]["proofs_verified"],
            "deal_policy": "bayer-groth-v1", "peer_closed": True, "owned_tasks_pending": pending,
            "callback_workers_alive": 0}
