"""The desktop launcher owns startup order and both sides of shutdown.

Controlled sidecar and game boundaries make failures deterministic. The
separate packaged acceptance test supplies the real Godot/native proof.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from holdem import desktop_launcher as launcher


@pytest.fixture
def paths(tmp_path, monkeypatch):
    # _run chooses a native runtime for its owned sidecar; preserve the
    # caller's environment after each test just as process exit would.
    monkeypatch.setenv("HOLDEM_LIBSODIUM", os.environ.get("HOLDEM_LIBSODIUM", ""))
    assets = tmp_path / "application files"
    assets.mkdir()
    files = [assets / name for name in
             ("PokerRuntime.exe", "poker.pck", "libsodium.dll")]
    for path in files:
        path.touch()
    return launcher.RuntimePaths(*files)


def _args(tmp_path, *extra):
    return launcher._parse_args(["--log-dir", str(tmp_path / "logs"), *extra])


def _presentation_summary():
    counts = ("menu_fresh_checks", "opening_preaction_checks", "shuffle_frames",
              "deal_frames", "shuffle_motion_checks", "deal_motion_checks",
              "moving_back_checks", "masked_control_checks", "private_card_checks",
              "next_hand_presentations", "bot_reaction_checks")
    return dict.fromkeys(counts, 1) | dict(
        ok=True, error="", presentations_completed=2, all_in_clicks=1,
        all_in_applied=True, cancelled_during_deal=True, returned_to_menu=True,
        menu_quiet_ms=3200, refused=[])


@pytest.mark.parametrize("missing", ["opening_preaction_checks", "deal_motion_checks"])
def test_presentation_diagnostic_rejects_success_without_an_observed_opening(missing):
    summary = _presentation_summary()
    launcher._validate_presentation_summary(summary)
    summary[missing] = 0
    with pytest.raises(RuntimeError, match="required outcome"):
        launcher._validate_presentation_summary(summary)


def test_presentation_diagnostic_launches_the_normal_desktop_flow(tmp_path, monkeypatch, paths):
    async def exercise():
        configured, commands, events = [], [], []

        async def sidecar(*, ready, **kwargs):
            configured.append(kwargs)
            ready(43139)
            await asyncio.Event().wait()

        def spawn(command, log_path):
            commands.append(command)
            launcher._write_json(log_path.parent / "presentation.json", _presentation_summary())
            return Game(events, code=0)

        monkeypatch.setattr(launcher, "run_sidecar", sidecar)
        monkeypatch.setattr(launcher, "_spawn_game", spawn)
        before = set(asyncio.all_tasks())
        args = _args(tmp_path, "--presentation-self-test")
        assert await asyncio.wait_for(launcher._run(args, paths), timeout=3) == 0
        assert configured[0]["desktop_lobby"] is True
        assert configured[0]["stack"] == 1000
        assert "res://sidecar/presentation_e2e.gd" in commands[0]
        assert "--headless" in commands[0]
        report = json.loads((args.log_dir / "presentation-self-test.json").read_text())
        assert report["ok"] is True
        _assert_no_owned_tasks_remain(before)

    asyncio.run(exercise())


@pytest.mark.parametrize("seconds", [29, 901, True, "300"])
def test_lan_diagnostic_budget_cannot_be_unbounded_or_shorter_than_a_real_turn(seconds):
    with pytest.raises(ValueError, match="between 30 and 900"):
        launcher._lan_check_timeout({"deadline_seconds": seconds})


def test_lan_diagnostic_budget_allows_real_timeouts_and_report_shutdown():
    assert launcher._lan_check_timeout({"deadline_seconds": 240}) == 255


@pytest.mark.parametrize("expected, evidence", [
    ("left_table", "left_table"), ("host_lost", "host_loss_seen"),
])
def test_departure_diagnostic_requires_its_observed_outcome(expected, evidence):
    config = {"expected_result": expected, "minimum_hands": 2, "target_hands": 10}
    summary = {"ok": True, "error": "", "hands_settled": 2, evidence: True}
    launcher._validate_lan_summary(config, summary)
    summary[evidence] = False
    with pytest.raises(RuntimeError, match="required outcome"):
        launcher._validate_lan_summary(config, summary)


@pytest.mark.parametrize("field, measured", [
    ("stall_action_hand", "action_timeout_elapsed_ms"),
    ("auto_next_wait_hand", "auto_next_elapsed_ms"),
])
def test_reliability_diagnostic_rejects_a_timer_probe_that_did_not_wait_thirty_seconds(field, measured):
    config = {"target_hands": 10, field: 2}
    summary = {"ok": True, "error": "", "hands_settled": 10, measured: 30000}
    launcher._validate_lan_summary(config, summary)
    summary[measured] = 500
    with pytest.raises(RuntimeError, match="required outcome"):
        launcher._validate_lan_summary(config, summary)


def test_reliability_diagnostic_requires_three_players_and_observed_departure():
    config = {"target_hands": 10, "expected_players": 3, "departed_name": "Third"}
    summary = {"ok": True, "error": "", "hands_settled": 10,
               "roster_names": ["Host", "Guest", "Third"], "departure_seen": True}
    launcher._validate_lan_summary(config, summary)
    for changed in ({"roster_names": ["Host", "Guest"]}, {"departure_seen": False}):
        with pytest.raises(RuntimeError, match="required outcome"):
            launcher._validate_lan_summary(config, summary | changed)


class Game:
    pid = 424242

    def __init__(self, events, code=None):
        self.events = events
        self.code = code
        self.closed = False

    def poll(self):
        return self.code

    def close(self):
        self.events.append("game closed")
        self.closed = True
        if self.code is None:
            self.code = -15


def _assert_no_owned_tasks_remain(before):
    remaining = [task for task in asyncio.all_tasks()
                 if task not in before and not task.done()]
    assert remaining == [], f"launcher left tasks running: {remaining}"


def test_game_starts_only_after_sidecar_readiness_and_gets_its_port(
        tmp_path, monkeypatch, paths):
    async def exercise():
        events = []
        started, release = asyncio.Event(), asyncio.Event()
        game = Game(events, code=0)
        commands = []

        async def sidecar(*, ready, port, **kwargs):
            assert port == 0, "launcher reserved a fixed sidecar port"
            events.append("sidecar starting")
            started.set()
            try:
                await release.wait()
                events.append("sidecar ready")
                ready(43127)
                await asyncio.Event().wait()
            finally:
                events.append("sidecar stopped")

        def spawn(command, log_path):
            assert events[-1] == "sidecar ready"
            commands.append(command)
            events.append("game started")
            return game

        monkeypatch.setattr(launcher, "run_sidecar", sidecar)
        monkeypatch.setattr(launcher, "_spawn_game", spawn)
        before = set(asyncio.all_tasks())
        run = asyncio.create_task(launcher._run(_args(tmp_path), paths))
        await asyncio.wait_for(started.wait(), timeout=2)
        assert commands == [], "game appeared before the sidecar was ready"
        release.set()
        assert await asyncio.wait_for(run, timeout=3) == 0
        assert game.closed
        assert "sidecar stopped" in events
        assert len(commands) == 1
        command = commands[0]
        assert str(paths.godot) == str(command[0])
        assert str(paths.pck) in [str(item) for item in command]
        assert "--sidecar-port=43127" in command
        _assert_no_owned_tasks_remain(before)

    asyncio.run(exercise())


def test_sidecar_startup_failure_never_spawns_a_game(tmp_path, monkeypatch, paths):
    async def sidecar(**kwargs):
        raise RuntimeError("controlled native startup failure")

    def forbidden(*args, **kwargs):
        pytest.fail("game was launched after failed sidecar startup")

    monkeypatch.setattr(launcher, "run_sidecar", sidecar)
    monkeypatch.setattr(launcher, "_spawn_game", forbidden)

    async def exercise():
        before = set(asyncio.all_tasks())
        with pytest.raises(RuntimeError, match="controlled native startup failure"):
            await asyncio.wait_for(launcher._run(_args(tmp_path), paths), timeout=3)
        _assert_no_owned_tasks_remain(before)

    asyncio.run(exercise())


def test_game_spawn_failure_stops_the_ready_sidecar(tmp_path, monkeypatch, paths):
    events = []

    async def sidecar(*, ready, **kwargs):
        try:
            ready(43128)
            await asyncio.Event().wait()
        finally:
            events.append("sidecar stopped")

    def failed(*args, **kwargs):
        raise OSError("controlled Godot launch failure")

    monkeypatch.setattr(launcher, "run_sidecar", sidecar)
    monkeypatch.setattr(launcher, "_spawn_game", failed)

    async def exercise():
        before = set(asyncio.all_tasks())
        with pytest.raises(OSError, match="controlled Godot launch failure"):
            await asyncio.wait_for(launcher._run(_args(tmp_path), paths), timeout=3)
        assert events == ["sidecar stopped"]
        _assert_no_owned_tasks_remain(before)

    asyncio.run(exercise())


@pytest.mark.parametrize("game_code", [0, 7])
def test_game_exit_is_propagated_and_stops_sidecar(
        game_code, tmp_path, monkeypatch, paths):
    events = []
    game = Game(events, code=game_code)

    async def sidecar(*, ready, **kwargs):
        try:
            ready(43129)
            await asyncio.Event().wait()
        finally:
            events.append("sidecar stopped")

    monkeypatch.setattr(launcher, "run_sidecar", sidecar)
    monkeypatch.setattr(launcher, "_spawn_game", lambda *args: game)

    async def exercise():
        before = set(asyncio.all_tasks())
        if game_code == 0:
            result = await asyncio.wait_for(launcher._run(_args(tmp_path), paths), timeout=3)
            assert result == 0
        else:
            with pytest.raises(RuntimeError, match=f"error {game_code}"):
                await asyncio.wait_for(launcher._run(_args(tmp_path), paths), timeout=3)
        assert game.closed and "sidecar stopped" in events
        session = json.loads((tmp_path / "logs" / "session.json").read_text())
        assert session["sidecar_port"] == 43129
        assert session["game_pid"] == game.pid
        assert session["game_exit_code"] == game_code
        _assert_no_owned_tasks_remain(before)

    asyncio.run(exercise())


def test_sidecar_failure_after_readiness_closes_the_game(
        tmp_path, monkeypatch, paths):
    async def exercise():
        events = []
        launched = asyncio.Event()
        game = Game(events)

        async def sidecar(*, ready, **kwargs):
            ready(43130)
            await launched.wait()
            raise RuntimeError("controlled running sidecar failure")

        def spawn(*args):
            launched.set()
            return game

        monkeypatch.setattr(launcher, "run_sidecar", sidecar)
        monkeypatch.setattr(launcher, "_spawn_game", spawn)
        before = set(asyncio.all_tasks())
        with pytest.raises(RuntimeError, match="controlled running sidecar failure"):
            await asyncio.wait_for(launcher._run(_args(tmp_path), paths), timeout=3)
        assert game.closed, "a failed sidecar left its game running"
        _assert_no_owned_tasks_remain(before)

    asyncio.run(exercise())


def test_supervisor_cancellation_closes_game_and_sidecar(tmp_path, monkeypatch, paths):
    async def exercise():
        events = []
        launched = asyncio.Event()
        game = Game(events)

        async def sidecar(*, ready, **kwargs):
            try:
                ready(43131)
                await asyncio.Event().wait()
            finally:
                events.append("sidecar stopped")

        def spawn(*args):
            launched.set()
            return game

        monkeypatch.setattr(launcher, "run_sidecar", sidecar)
        monkeypatch.setattr(launcher, "_spawn_game", spawn)
        before = set(asyncio.all_tasks())
        run = asyncio.create_task(launcher._run(_args(tmp_path), paths))
        await asyncio.wait_for(launched.wait(), timeout=2)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        assert game.closed and "sidecar stopped" in events
        _assert_no_owned_tasks_remain(before)

    asyncio.run(exercise())


def test_frozen_resources_come_from_bundle_even_with_developer_overrides(
        tmp_path, monkeypatch):
    bundle = tmp_path / "relocated bundle with spaces"
    for relative in ("assets/PokerRuntime.exe", "assets/poker.pck", "native/libsodium.dll"):
        target = bundle / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setenv("GODOT_BIN", str(tmp_path / "developer godot.exe"))
    monkeypatch.setenv("HOLDEM_LIBSODIUM", str(tmp_path / "developer libsodium.dll"))
    monkeypatch.chdir(tmp_path)
    args = _args(tmp_path, "--godot", "wrong-godot.exe", "--pck", "wrong.pck",
                 "--libsodium", "wrong.dll")
    resolved = launcher.resolve_paths(args)
    assert resolved.godot == bundle / "assets/PokerRuntime.exe"
    assert resolved.pck == bundle / "assets/poker.pck"
    assert resolved.libsodium == bundle / "native/libsodium.dll"


def test_source_preview_uses_the_explicit_resource_paths(tmp_path, monkeypatch, paths):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    args = _args(tmp_path, "--godot", str(paths.godot), "--pck", str(paths.pck),
                 "--libsodium", str(paths.libsodium))
    resolved = launcher.resolve_paths(args)
    assert resolved == paths


def test_cli_startup_failure_is_nonzero_and_leaves_a_useful_report(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    logs = tmp_path / "startup failure logs"
    result = subprocess.run(
        [sys.executable, "-B", "-m", "holdem.desktop_launcher", "--self-test",
         "--log-dir", str(logs), "--godot", str(tmp_path / "missing-game.exe"),
         "--pck", str(tmp_path / "missing.pck"),
         "--libsodium", str(tmp_path / "missing.dll")],
        cwd=tmp_path, env=dict(os.environ, PYTHONPATH=str(repo)),
        capture_output=True, text=True, timeout=10)
    assert result.returncode != 0, result.stdout + result.stderr
    report = json.loads((logs / "self-test.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert "missing-game.exe" in report["error"]
    assert report["summary"] is None
    assert not (logs / "game.log").exists(), "startup failure still launched a game"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process-job ownership")
def test_windows_game_owner_closes_its_actual_child(tmp_path):
    from test_windows_package import _process_is_running

    log_path = tmp_path / "owned game.log"
    game = launcher._spawn_game(
        [sys.executable, "-u", "-c",
         "import time; print('OWNED_GAME_READY', flush=True); time.sleep(30)"],
        log_path)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if "OWNED_GAME_READY" in log_path.read_text(errors="replace"):
                break
            assert game.poll() is None, log_path.read_text(errors="replace")
            time.sleep(0.01)
        else:
            pytest.fail("the owned game did not reach its ready marker")
        assert _process_is_running(game.pid)
    finally:
        game.close()
    assert game.poll() is not None
    assert not _process_is_running(game.pid), "closing the job left its own child alive"
    game.close()  # Repeated cleanup is safe after normal supervisor unwind.


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process-job ownership")
def test_windows_parent_exit_closes_child_without_python_cleanup(tmp_path):
    from test_windows_package import _process_is_running

    script = (
        "import json, os, sys; from pathlib import Path; "
        "from holdem.desktop_launcher import _spawn_game; "
        "game = _spawn_game([sys.executable, '-c', 'import time; time.sleep(5)'], "
        "Path(sys.argv[2])); "
        "Path(sys.argv[1]).write_text(json.dumps({'pid': game.pid})); "
        "os._exit(0)"
    )
    record = tmp_path / "owned-child.json"
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", script, str(record), str(tmp_path / "game.log")],
        cwd=tmp_path, env=dict(os.environ, PYTHONPATH=str(repo)),
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    pid = json.loads(record.read_text())["pid"]
    # The child would naturally remain alive for five seconds. Windows must
    # remove it immediately when its parent's private job handle disappears.
    deadline = time.monotonic() + 2
    while _process_is_running(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _process_is_running(pid), "abrupt launcher exit orphaned its game"
