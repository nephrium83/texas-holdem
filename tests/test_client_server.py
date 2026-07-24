"""The Godot-facing socket layer: a real Session served over localhost
TCP, exercised by a raw newline-JSON client standing in for the Godot
front end. GODOT_PROTOCOL.md sections 2, 4, 5 and 7 over an actual
socket, with the full hostless machinery (mental deal + replica betting)
running underneath on the in-memory bus.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.session import Session
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.replica_table import PHASE_BETTING
from holdem.client_server import ClientServer, PROTOCOL_VERSION

import importlib
try:
    importlib.import_module("holdem.p2p.elgamal")   # libsodium guard
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)

RECV_TIMEOUT = 5


def make_sessions(n):
    """N real Sessions on an in-memory bus, no hand begun (lobby)."""
    bus = InMemoryBus()
    order = [f"peer{i}" for i in range(n)]
    sessions = {}
    for i, cid in enumerate(order):
        s = Session(is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
                    transport=InMemoryTransport(bus, cid))
        s.local_conn_id = cid
        s._seat_order = list(order)
        bus.register(cid, s)
        sessions[cid] = s
    return bus, sessions, order


def make_table(n, stacks=None, sb=5, bb=10, hand=1, button=0):
    """Same, with one hostless hand started and dealt (mirrors
    tests/test_session_hand.py)."""
    bus, sessions, order = make_sessions(n)
    names = [f"P{i}" for i in range(n)]
    stacks = list(stacks) if stacks else [500] * n
    for cid in order:
        sessions[cid].start_p2p_hand(hand_no=hand, names=names,
                                     stacks=stacks, sb=sb, bb=bb,
                                     button=button)
    bus.drain()
    return bus, sessions, order


class Wire:
    """Minimal Godot stand-in: newline-delimited JSON over TCP."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    @classmethod
    async def connect(cls, port):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        return cls(r, w)

    async def recv(self):
        line = await asyncio.wait_for(self.reader.readline(), RECV_TIMEOUT)
        assert line, "server closed the connection"
        return json.loads(line)

    async def last_snapshot(self, quiet=0.3):
        """Drain pending messages; return the newest snapshot seen."""
        last = None
        while True:
            try:
                line = await asyncio.wait_for(self.reader.readline(), quiet)
            except asyncio.TimeoutError:
                return last
            if not line:
                return last
            obj = json.loads(line)
            if obj.get("type") == "snapshot":
                last = obj

    async def command(self, name, **payload):
        obj = {"type": "command", "command": name}
        if payload:
            obj["payload"] = payload
        self.writer.write(json.dumps(obj).encode() + b"\n")
        await self.writer.drain()

    def close(self):
        self.writer.close()


async def serve(session):
    srv = ClientServer(session)          # port=0: ephemeral
    await srv.start()
    return srv


async def open_client(srv):
    """Connect and consume the section-7 greeting: hello, then snapshot."""
    w = await Wire.connect(srv.port)
    hello = await w.recv()
    assert hello == {"type": "hello", "protocol": PROTOCOL_VERSION}
    snap = await w.recv()
    assert snap["type"] == "snapshot"
    return w, snap


def test_lobby_snapshot_before_any_hand():
    async def inner():
        bus, sessions, order = make_sessions(2)
        srv = await serve(sessions[order[0]])
        try:
            w, snap = await open_client(srv)
            assert snap["phase"] == "lobby"
            assert [s["seat"] for s in snap["seats"]] == [0, 1]
            assert snap["seats"][0]["is_you"] is True
            assert snap["you"]["seat"] == 0
            w.close()
        finally:
            await srv.stop()
    asyncio.run(inner())


def test_connect_mid_hand_gets_full_betting_snapshot():
    async def inner():
        bus, sessions, order = make_table(2)
        srv = await serve(sessions[order[0]])
        try:
            w, snap = await open_client(srv)
            assert snap["phase"] == "betting"
            assert snap["seat"] == 0
            assert len(snap["you"]["hole"]) == 2
            # no-leak over the wire: nobody else's hole cards in a live hand
            assert all("hole" not in s for s in snap["seats"])
            w.close()
        finally:
            await srv.stop()
    asyncio.run(inner())


def test_command_applies_then_fresh_snapshot_and_peers_sync():
    async def inner():
        bus, sessions, order = make_table(2)
        actor = sessions[order[0]]._replica.actor
        srv = await serve(sessions[order[actor]])
        try:
            w, snap0 = await open_client(srv)
            assert "legal" in snap0["you"]           # our turn: bounds present
            await w.command("check_call")
            res = await w.recv()
            assert res["type"] == "command_result"
            assert res["ok"] is True and res["verdict"] == "applied"
            snap1 = await w.recv()
            assert snap1["type"] == "snapshot"
            assert snap1["action_on"] != actor       # turn moved on
            bus.drain()                              # peers hear the action
            digests = {sessions[c]._replica.state_digest() for c in order}
            assert len(digests) == 1                 # replicas stay in sync
            w.close()
        finally:
            await srv.stop()
    asyncio.run(inner())


def test_out_of_turn_command_rejected_never_trusted():
    async def inner():
        bus, sessions, order = make_table(2)
        actor = sessions[order[0]]._replica.actor
        other = 1 - actor
        srv = await serve(sessions[order[other]])
        try:
            w, snap0 = await open_client(srv)
            assert "legal" not in snap0["you"]       # not our turn
            await w.command("fold")
            res = await w.recv()
            assert res["ok"] is False and res["verdict"] == "rejected"
            snap1 = await w.recv()                   # snapshot still follows
            assert snap1["type"] == "snapshot"
            assert snap1["phase"] == "betting"       # nothing moved
            w.close()
        finally:
            await srv.stop()
    asyncio.run(inner())


def test_bad_commands_answer_with_error():
    async def inner():
        bus, sessions, order = make_table(2)
        actor = sessions[order[0]]._replica.actor
        srv = await serve(sessions[order[actor]])
        try:
            w, _ = await open_client(srv)
            await w.command("jazzhands")             # unknown command
            res = await w.recv()
            assert res["ok"] is False and "error" in res
            assert "verdict" not in res
            assert (await w.recv())["type"] == "snapshot"
            await w.command("raise_to")              # missing amount
            res = await w.recv()
            assert res["ok"] is False and "error" in res
            assert (await w.recv())["type"] == "snapshot"
            w.close()
        finally:
            await srv.stop()
    asyncio.run(inner())


def test_unprompted_push_when_a_remote_player_acts():
    async def inner():
        bus, sessions, order = make_table(2)
        actor = sessions[order[0]]._replica.actor
        other = 1 - actor
        srv = await serve(sessions[order[other]])    # we watch the non-actor
        try:
            w, _ = await open_client(srv)
            verdict = sessions[order[actor]].send_bet_action("call")
            assert verdict == "applied"
            bus.drain()          # delivers to the watched session -> hook
            snap = await w.recv()                    # unsolicited push
            assert snap["type"] == "snapshot"
            acted = snap["seats"][actor]["last_action"]
            assert acted != ""                       # the action is visible
            w.close()
        finally:
            await srv.stop()
    asyncio.run(inner())


def test_fold_out_over_the_socket_settles_and_reveals_nothing():
    async def inner():
        bus, sessions, order = make_table(2)
        actor = sessions[order[0]]._replica.actor
        srv = await serve(sessions[order[actor]])
        try:
            w, _ = await open_client(srv)
            await w.command("fold")
            res = await w.recv()
            assert res["ok"] is True
            await w.recv()                           # post-command snapshot
            bus.drain()          # peers' audit shares complete settlement
            snap = await w.last_snapshot()           # pushed after the pump
            assert snap is not None and snap["phase"] == "settled"
            assert snap["result"] is not None
            assert snap["result"]["runs"] == []      # fold-out: no showdown
            assert all("hole" not in s for s in snap["seats"])
            w.close()
        finally:
            await srv.stop()
    asyncio.run(inner())


def test_contested_showdown_reveal_reaches_the_watcher():
    async def inner():
        bus, sessions, order = make_table(2)
        watch = 0
        srv = await serve(sessions[order[watch]])
        try:
            w, _ = await open_client(srv)
            # Drive a full checkdown via the sessions themselves; the
            # watcher's client should end up with the settled snapshot.
            while sessions[order[0]]._replica.phase == PHASE_BETTING:
                seat = sessions[order[0]]._replica.actor
                assert sessions[order[seat]].send_bet_action("call") \
                    == "applied"
                bus.drain()
            snap = await w.last_snapshot()
            assert snap is not None and snap["phase"] == "settled"
            assert snap["result"]["runs"]            # contested showdown
            for s in snap["seats"]:
                if s["is_you"]:
                    assert "hole" not in s           # yours live in you.hole
                else:
                    assert len(s["hole"]) == 2       # audit made it public
            assert len(snap["you"]["hole"]) == 2
            w.close()
        finally:
            await srv.stop()
    asyncio.run(inner())


def test_next_hand_command_over_the_socket_starts_hand_two():
    """A client drives continuous play over the wire: after a hand settles,
    its next_hand command starts hand 2, and the follow-up snapshot shows
    the new hand. Every peer's sidecar advances (driven here directly for
    the non-watched seats)."""
    async def inner():
        bus, sessions, order = make_table(3)
        watch = 0
        srv = await serve(sessions[order[watch]])
        try:
            w, _ = await open_client(srv)
            # Settle hand 1 (checkdown) via the sessions.
            while sessions[order[0]]._replica.phase == PHASE_BETTING:
                seat = sessions[order[0]]._replica.actor
                assert sessions[order[seat]].send_bet_action("call") \
                    == "applied"
                bus.drain()
            settled = await w.last_snapshot()
            assert settled is not None and settled["phase"] == "settled"
            assert settled["session_over"] is False

            # The OTHER peers advance directly; the watched peer advances via
            # its client's next_hand command.
            for cid in order[1:]:
                assert sessions[cid].next_p2p_hand() == "started"
            await w.command("next_hand")
            res = await w.recv()
            assert res["command"] == "next_hand"
            assert res["verdict"] == "started" and res["ok"] is True
            bus.drain()

            assert sessions[order[watch]]._hand_no == 2
            snap = await w.last_snapshot()
            assert snap is not None
            assert snap["hand_num"] == 2
            assert snap["phase"] in ("dealing", "betting")
            w.close()
        finally:
            await srv.stop()
    asyncio.run(inner())


def test_stop_restores_the_exact_previous_state_hook():
    """Stopping a sidecar server must not strand its bound callback."""
    async def inner():
        _, sessions, order = make_sessions(2)
        session = sessions[order[0]]
        calls = []
        previous = lambda: calls.append("previous")
        session.on_state_changed = previous
        srv = await serve(session)
        assert session.on_state_changed is srv._installed_hook

        session._notify_state_changed()
        await asyncio.sleep(0)
        assert calls == ["previous"]

        await srv.stop()
        assert session.on_state_changed is previous
        session._notify_state_changed()
        assert calls == ["previous", "previous"]
    asyncio.run(inner())


def test_client_server_rejects_double_start_and_can_restart():
    async def inner():
        _, sessions, order = make_sessions(2)
        session = sessions[order[0]]
        srv = ClientServer(session)
        await srv.start()
        with pytest.raises(RuntimeError, match="already started"):
            await srv.start()
        await srv.stop()

        await srv.start()
        try:
            wire_client, snap = await open_client(srv)
            assert snap["phase"] == "lobby"
            wire_client.close()
        finally:
            await srv.stop()
    asyncio.run(inner())
