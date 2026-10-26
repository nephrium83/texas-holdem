"""Desktop composition owns the Internet facade without bypassing its Session owner."""
from __future__ import annotations

import asyncio
import sys
import threading
from types import SimpleNamespace

import pytest

from holdem.desktop_table import DesktopTable


class _InternetFacade:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        self.closed = 0
        self.start_verdict = "not_ready"
        self.view = {"type": "snapshot", "phase": "lobby", "owner_view": True,
                     "lobby": {"mode": "host", "network": "internet", "state": "connecting"}}
        self.instances.append(self)

    @property
    def session(self):
        raise AssertionError("Desktop composition must not access the Internet Session")

    def host(self):
        self.calls.append(("host",))

    def join(self, code):
        if code == "invalid":
            raise ValueError("Invalid Internet invite")
        self.calls.append(("join", code))

    def start(self):
        self.calls.append(("start",))
        return self.start_verdict

    def set_ready(self, ready):
        self.calls.append(("set_ready", ready))

    def command(self, command, payload):
        self.calls.append((command, payload))
        return {"type": "command_result", "command": command, "ok": True, "verdict": "applied"}

    def snapshot(self):
        return self.view

    def close(self):
        self.closed += 1


@pytest.fixture
def facade(monkeypatch):
    _InternetFacade.instances = []
    monkeypatch.setitem(sys.modules, "holdem.internet_session", SimpleNamespace(InternetTable=_InternetFacade))
    return _InternetFacade


def _desktop(**kwargs):
    return DesktopTable(seats=2, nickname="Original", settings={"stack": 10000},
                        on_change=lambda: None, **kwargs)


@pytest.mark.parametrize("command", ["host_internet", "join_internet"])
def test_internet_creation_and_game_commands_use_the_owner_facade(facade, command):
    loop, services = object(), {"test": "configured endpoint"}
    table = _desktop(loop=loop, internet_services=services)
    try:
        payload = {"nickname": " Ada ", "invite": "private-code"}
        assert table.apply_command(command, payload)["verdict"] == "created"
        peer = facade.instances[0]
        assert peer.kwargs["loop"] is loop and peer.kwargs["services"] is services
        assert peer.kwargs["nickname"] == "Ada" and peer.kwargs["settings"] == {"stack": 10000}
        assert peer.calls == ([("host",)] if command == "host_internet" else [("join", "private-code")])
        assert table.snapshot() is peer.view
        assert table.apply_command("set_ready", {"ready": True})["ok"]
        assert peer.calls[-1] == ("set_ready", True)
        with pytest.raises(ValueError, match="true/false"):
            table.apply_command("set_ready", {"ready": 1})
        assert table.apply_command("start_game", {}) == {
            "type": "command_result", "command": "start_game", "ok": False, "verdict": "not_ready"}
        peer.start_verdict = "started"
        assert table.apply_command("start_game", {})["ok"]
        assert table.apply_command("raise_to", {"amount": 100})["ok"]
        assert peer.calls[-1] == ("raise_to", {"amount": 100})
        assert table.apply_command("leave_table", {})["verdict"] == "left"
        assert peer.closed == 1 and table.snapshot()["lobby"]["mode"] == "menu"
        table.apply_command("host_internet", {"nickname": "Again"})
        assert len(facade.instances) == 2
    finally:
        table.close()
        table.close()
    assert [peer.closed for peer in facade.instances] == [1, 1]


def test_missing_loop_and_invalid_invite_preserve_the_menu(facade):
    table = _desktop()
    with pytest.raises(ValueError, match="Internet play is unavailable"):
        table.apply_command("host_internet", {"nickname": "Ada"})
    assert not facade.instances
    table.close()
    table = _desktop(loop=object())
    try:
        before = table.snapshot()
        with pytest.raises(ValueError, match="Invalid Internet invite"):
            table.apply_command("join_internet", {"nickname": "Changed", "invite": "invalid"})
        assert facade.instances[0].closed == 1
        assert table.snapshot() == before
    finally:
        table.close()


def test_current_internet_table_is_preserved_until_explicit_leave(facade):
    table = _desktop(loop=object())
    try:
        table.apply_command("host_internet", {"nickname": "Ada"})
        peer = facade.instances[0]
        for command in ("local_game", "host_lan", "join_lan", "host_internet", "join_internet"):
            with pytest.raises(ValueError, match="Leave the current table"):
                table.apply_command(command, {"nickname": "Other", "invite": "invalid"})
            assert table.snapshot() is peer.view and peer.closed == 0
        assert len(facade.instances) == 1
    finally:
        table.close()
    for command in ("host_internet", "join_internet"):
        with pytest.raises(ValueError, match="application is closing"):
            table.apply_command(command, {"nickname": "Ada"})


def test_sidecar_supplies_its_running_loop_and_closes_composition_off_loop(monkeypatch):
    from holdem import desktop_table, sidecar_launcher
    captured = {}

    class Table:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def snapshot(self):
            return {}

        def apply_command(self, *_args):
            return {}

        def close(self):
            captured["close_thread"] = threading.get_ident()

    class Server:
        port = 1234

        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self):
            captured["active_loop"] = asyncio.get_running_loop()
            captured["loop_thread"] = threading.get_ident()

        async def stop(self):
            captured["server_stopped"] = True

        def notify_state_changed(self):
            pass

    def ready(_port):
        raise RuntimeError("owned test shutdown")

    monkeypatch.setattr(desktop_table, "DesktopTable", Table)
    monkeypatch.setattr(sidecar_launcher, "ClientServer", Server)
    with pytest.raises(RuntimeError, match="owned test shutdown"):
        asyncio.run(sidecar_launcher._run_desktop_lobby(
            seats=2, sb=25, bb=50, stack=1000, structure="No-Limit", port=0,
            seed=None, nickname="Ada", ready=ready))
    assert captured["loop"] is captured["active_loop"]
    assert captured["close_thread"] != captured["loop_thread"]
    assert captured["server_stopped"] is True
