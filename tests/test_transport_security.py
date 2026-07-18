"""Security regression tests for the P2P layer.

Covers the fixes from the 2026-07-17 audit:
- C-1: transport requires a valid signed envelope on every peer message
- C-1: seat/pubkey binding is enforced in handle_game_action
- H-1: shuffle reveals broadcast the real nonce and peers verify them
- hash-chain: enforcing signatures must not drop consecutive messages
"""
import asyncio
import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p import transport as t, wire
from holdem.p2p.session import Session
from holdem.p2p.shuffle import ShuffleRound, verify_commit


def _framed(d: dict) -> bytes:
    b = json.dumps(d).encode()
    return struct.pack(">I", len(b)) + b


class _FakeReader:
    def __init__(self, blob: bytes):
        self.blob, self.pos = blob, 0

    async def readexactly(self, n: int) -> bytes:
        chunk = self.blob[self.pos:self.pos + n]
        if len(chunk) < n:
            raise asyncio.IncompleteReadError(chunk, n)
        self.pos += n
        return chunk


def _read(blob: bytes) -> dict:
    return asyncio.new_event_loop().run_until_complete(
        t._read_msg(_FakeReader(blob)))


# --------------------------------------------------------------- C-1 transport

def test_unsigned_game_message_rejected():
    with pytest.raises(ValueError):
        _read(_framed({"type": "kick", "payload": {"seat": 2}}))


def test_signed_message_accepted():
    signed = json.loads(wire.pack("action",
                                  {"seat": 1, "action": "raise", "amount": 40}))
    msg = _read(_framed(signed))
    assert msg["type"] == "action"
    assert msg["payload"]["amount"] == 40


def test_tampered_payload_rejected():
    env = json.loads(wire.pack("action", {"seat": 1, "amount": 40}))
    env["payload"]["amount"] = 999999
    with pytest.raises(ValueError):
        _read(_framed(env))


def test_relay_control_frame_allowed_unsigned():
    m = _read(_framed({"type": "relay_join", "room": "ABCD", "peer_id": "00"}))
    assert m["type"] == "relay_join"


def test_sign_frame_roundtrips():
    blob = t._sign_frame({"type": "game_state", "payload": {"pot": 100}})
    back = wire.unpack(blob[4:])
    assert back["payload"]["pot"] == 100


def test_presigned_dict_not_double_wrapped():
    pre = json.loads(wire.pack("chat", {"text": "hi"}))
    blob = t._sign_frame(pre)
    back = wire.unpack(blob[4:])
    assert back["payload"]["text"] == "hi"


# ------------------------------------------------------------ C-1 seat binding

def test_action_rejects_wrong_seat_owner():
    """A peer on conn 'B' cannot submit an action claiming seat 0 (owned by 'A')."""
    s = Session(is_host=True, nickname="H", avatar_b64="")

    class _Eng:
        actor = 0
    s._engine = _Eng()
    s._seat_order = ["A", "B"]
    s._VALID_ACTIONS = {"fold", "call", "raise", "check", "bet"}

    fired = []
    s.on_action = lambda seat, act, amt: fired.append((seat, act, amt))

    # Correct owner acts on seat 0 -> accepted
    s.handle_game_action("A", {"payload": {"seat": 0, "action": "call"}})
    assert fired == [(0, "call", 0)]

    # Impostor on conn B claims seat 0 -> rejected
    fired.clear()
    s.handle_game_action("B", {"payload": {"seat": 0, "action": "raise",
                                           "amount": 50}})
    assert fired == []


# ------------------------------------------------------------- hash-chain

def test_consecutive_signed_messages_deliver():
    """Enforcing signatures must not trip the chain guard for messages that
    carry the genesis prev (senders do not yet thread prev)."""
    s = Session(is_host=True, nickname="H", avatar_b64="")
    s._host_conn_id = "peerA"
    got = []
    s._on_chat = lambda cid, msg: got.append(msg["payload"]["text"])
    s.handle_message("peerA", json.loads(wire.pack("chat", {"text": "one"})))
    s.handle_message("peerA", json.loads(wire.pack("chat", {"text": "two"})))
    assert got == ["one", "two"]


# --------------------------------------------------------------------- H-1

def _paired_rounds():
    ids = ["host", "peerB"]
    host = ShuffleRound(local_conn_id="host",  all_conn_ids=ids)
    peer = ShuffleRound(local_conn_id="peerB", all_conn_ids=ids)
    host.local_commit(); peer.local_commit()
    host.record_commit("peerB", peer._commits["peerB"])
    peer.record_commit("host", host._commits["host"])
    host.record_reveal("peerB", peer._seeds["peerB"], peer._nonces["peerB"])
    peer.record_reveal("host",  host._seeds["host"],  host._nonces["host"])
    return host, peer


def test_reveal_snapshot_uses_real_nonce():
    host, _ = _paired_rounds()
    snap = host.reveals_snapshot()
    for cid, r in snap.items():
        seed  = bytes.fromhex(r["seed_hex"])
        nonce = bytes.fromhex(r["nonce_hex"])
        assert verify_commit(seed, nonce, host._commits[cid])


def test_peers_derive_identical_deck():
    host, peer = _paired_rounds()
    assert host.shuffled_deck() == peer.shuffled_deck()


def test_peer_flags_tampered_reveal():
    host, peer = _paired_rounds()
    snap = host.reveals_snapshot()
    sess = Session(is_host=False, nickname="P", avatar_b64="")
    sess._shuffle_round = peer
    ready, cheat = [], []
    sess.on_shuffle_ready = lambda deck: ready.append(deck)
    sess.on_shuffle_cheat = lambda cid: cheat.append(cid)

    sess._on_shuffle_reveal_collect("host", {"payload": {"reveals": snap}})
    assert ready and not cheat

    bad = {k: dict(v) for k, v in snap.items()}
    seed = bytearray(bytes.fromhex(bad["host"]["seed_hex"]))
    seed[0] ^= 0xFF
    bad["host"]["seed_hex"] = bytes(seed).hex()
    ready.clear(); cheat.clear()
    sess._on_shuffle_reveal_collect("host", {"payload": {"reveals": bad}})
    assert cheat and not ready


if __name__ == "__main__":
    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for name, fn in fns:
        fn()
        print(f"  {name}: ok")
    print(f"ALL PASS ({len(fns)} tests)")
