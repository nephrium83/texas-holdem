"""Concurrency and composition contracts shared by local and LAN rendering."""
from __future__ import annotations

from copy import deepcopy
import threading

import pytest

from holdem import client_view
from holdem.desktop_table import DesktopTable
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.session import Session
from holdem.p2p.timeout import FakeClock

SETTINGS = dict(sb=25, bb=50, stack=1000, structure="No-Limit",
                deal_policy=Session.DEAL_POLICY_BG)


def _session(*, is_host=True):
    bus = InMemoryBus()
    session = Session(is_host=is_host, nickname="Alice", avatar_b64="",
                      transport=InMemoryTransport(bus, "you"),
                      clock=FakeClock(), master_secret=bytes(range(32)))
    session.local_conn_id = "you"
    bus.register("you", session)
    return session


def test_public_read_state_blocks_remote_mutation_and_is_reentrant():
    session = _session()
    attempted = threading.Event()
    finished = threading.Event()

    def stop_from_transport():
        attempted.set()
        session.terminate(Session.HOST_LOST, "remote host disappeared")
        finished.set()

    with session.read_state() as view:
        assert view is session
        with session.read_state():
            assert session.terminal_state is None
        worker = threading.Thread(target=stop_from_transport)
        worker.start()
        assert attempted.wait(2)
        assert not finished.wait(0.05), "remote mutation crossed an atomic read"
        assert session.terminal_state is None
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert session.terminal_state == Session.HOST_LOST


def test_start_ready_game_holds_owner_between_ready_check_and_start(monkeypatch):
    session = _session()
    session.seat_local_table(["you", "guest"])
    session.set_ready("you", True)
    session.set_ready("guest", True)
    at_start = threading.Event()
    allow_start = threading.Event()
    attempting_unready = threading.Event()
    unready_finished = threading.Event()
    original = Session.start_game
    verdicts = []

    def pause_start(self, settings):
        at_start.set()
        assert allow_start.wait(2)
        return original(self, settings)

    monkeypatch.setattr(Session, "start_game", pause_start)

    def unready():
        attempting_unready.set()
        session.set_ready("guest", False)
        unready_finished.set()

    starter = threading.Thread(target=lambda: verdicts.append(
        session.start_ready_game(SETTINGS)))
    starter.start()
    try:
        assert at_start.wait(2)
        guest = threading.Thread(target=unready)
        guest.start()
        assert attempting_unready.wait(2)
        assert not unready_finished.wait(0.05), "readiness changed inside the start gate"
    finally:
        allow_start.set()
    starter.join(timeout=2)
    guest.join(timeout=2)
    assert not starter.is_alive() and not guest.is_alive()
    assert verdicts == ["started"]
    assert session.state == "PLAYING"
    assert session.seat_order == ["you", "guest"]


def _roster(session, entries):
    session.handle_message("host-hop", {
        "type": "player_list", "payload": {"players": entries},
    })


def _player(cid, key, *, host=False):
    return dict(conn_id=cid, nickname=cid, ready=True, is_host=host,
                ed25519_pubkey_hex=key.hex())


def test_departed_lobby_members_disappear_without_forgetting_their_signing_key():
    session = _session(is_host=False)
    host = _player("host", bytes([1]) * 32, host=True)
    you = _player("you", bytes([2]) * 32)
    original_key = bytes([3]) * 32
    guest = _player("guest", original_key)
    _roster(session, [host, you, guest])
    assert len(client_view.snapshot(session)["seats"]) == 3
    _roster(session, [host, you])
    assert [p.conn_id for p in session.player_list()] == ["host", "you"]
    assert len(client_view.snapshot(session)["seats"]) == 2
    _roster(session, [host, you, _player("guest", bytes([9]) * 32)])
    restored = next(p for p in session.player_list() if p.conn_id == "guest")
    assert restored.ed25519_pubkey_hex == original_key.hex()


def test_roster_updates_do_not_remove_frozen_playing_members():
    session = _session(is_host=False)
    host = _player("host", bytes([1]) * 32, host=True)
    you = _player("you", bytes([2]) * 32)
    _roster(session, [host, you])
    session.handle_message("host-hop", {
        "type": "game_start",
        "payload": {"seat_order": ["host", "you"], "table_settings": SETTINGS},
    })
    assert session.state == "PLAYING"
    _roster(session, [host])
    assert [p.conn_id for p in session.player_list()] == ["host", "you"]
    assert session.seat_order == ["host", "you"]


def test_rendered_settlement_is_detached_from_nested_protocol_results():
    from crypto_gate import require_crypto
    require_crypto()
    from holdem.sidecar_launcher import _make_sessions, _wire_hand_start

    bus, sessions, order = _make_sessions(2, nickname="Alice")
    _wire_hand_start(sessions, order)
    host = sessions[order[0]]
    host.start_game(SETTINGS)
    bus.drain()
    for _ in range(20):
        if host.hand_result is not None:
            break
        actor = host.replica.actor
        assert actor is not None
        assert sessions[order[actor]].send_bet_action("call") == "applied"
        bus.drain()
    assert host.hand_result is not None
    rendered = client_view.snapshot(host)
    result_before = deepcopy(host.hand_result)
    assert rendered["result"]["pots"]
    rendered["result"]["pots"][0]["amount"] += 123
    rendered["result"]["shown"].append(99)
    assert host.hand_result == result_before
    assert client_view.snapshot(host)["result"] == result_before


def _desktop():
    return DesktopTable(seats=2, nickname="Alice", settings=SETTINGS,
                        seed=7, on_change=lambda: None)


def test_desktop_local_leave_and_create_again_are_complete_transitions():
    table = _desktop()
    try:
        assert table.snapshot()["lobby"]["mode"] == "menu"
        with pytest.raises(ValueError, match="Choose a game first"):
            table.apply_command("start_game", {})
        created = table.apply_command("local_game", {"nickname": "Alice"})
        assert created["ok"]
        assert table.snapshot()["lobby"]["mode"] == "local"
        assert len(table.snapshot()["lobby"]["players"]) == 2
        with pytest.raises(ValueError, match="Leave the current table"):
            table.apply_command("host_lan", {"nickname": "Alice"})
        assert table.apply_command("leave_table", {})["verdict"] == "left"
        assert table.snapshot()["lobby"]["mode"] == "menu"
        table.apply_command("local_game", {"nickname": "Bob"})
        assert table.snapshot()["lobby"]["nickname"] == "Bob"
    finally:
        table.close()


def test_desktop_shutdown_cannot_be_reopened_by_late_local_commands():
    table = _desktop()
    table.apply_command("local_game", {"nickname": "Alice"})
    table.close()
    table.close()
    for command in ("local_game", "host_lan", "join_lan", "leave_table"):
        with pytest.raises(ValueError, match="application is closing"):
            table.apply_command(command, {"nickname": "Alice"})
    assert table.snapshot()["lobby"]["mode"] == "menu"
