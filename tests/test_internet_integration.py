"""Three real Godot clients use Internet buttons and keep playing without setup."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time

import pytest

pytest.importorskip("aiortc")
pytest.importorskip("websockets")
from crypto_gate import require_crypto
from holdem import native_crypto
from holdem.introduction_service import IntroductionService
from tests.internet_peer import InternetDesktopPeer, ROOT


GODOT = os.environ.get("GODOT_BIN") or shutil.which("godot4") or shutil.which("godot")


@pytest.fixture(params=["source", "frozen"])
def application(request, tmp_path):
    if request.param == "frozen":
        selected = os.environ.get("HOLDEM_WINDOWS_EXE")
        if not selected:
            pytest.skip("set HOLDEM_WINDOWS_EXE for frozen three-player Internet UI acceptance")
        executable = Path(selected).resolve(strict=True)
        relocated = tmp_path / "Relocated Internet Poker"
        shutil.copytree(executable.parent, relocated)
        from tests.test_windows_package import build
        build.verify_manifest(relocated)
        return {"mode": "frozen", "executable": relocated / executable.name}
    if GODOT is None:
        pytest.skip("set GODOT_BIN for source three-player Internet UI acceptance")
    require_crypto()
    backend = native_crypto.initialize()
    selected = getattr(getattr(backend, "_library", None), "_name", None)
    if not selected or not Path(selected).is_file():
        pytest.skip("source desktop acceptance needs an explicit loaded libsodium file")
    stage = tmp_path / "staged Godot resources"
    shutil.copytree(ROOT / "godot", stage,
                    ignore=shutil.ignore_patterns(".godot", "addons", "test", "*.tmp"))
    project = stage / "project.godot"
    project.write_text(re.sub(r"(?ms)^\[editor_plugins\]\s*.*?(?=^\[|\Z)", "",
                             project.read_text(encoding="utf-8")), encoding="utf-8")
    pck = tmp_path / "source-test.pck"
    for command in ([GODOT, "--headless", "--path", str(stage), "--import"],
                    [GODOT, "--headless", "--path", str(stage), "--export-pack", "Windows Preview Pack", str(pck)]):
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        assert result.returncode == 0, "Could not prepare the temporary Godot resource pack"
    assert pck.is_file()
    return {"mode": "source", "godot": Path(GODOT).resolve(), "pck": pck, "sodium": Path(selected).resolve()}


async def _wait(peers, path, label, timeout=190):
    deadline = time.monotonic() + timeout
    while True:
        values = [peer.read(getattr(peer, path)) for peer in peers]
        if all(value is not None for value in values):
            return values
        for peer in peers:
            peer.check_running()
        if time.monotonic() >= deadline:
            raise AssertionError(label)
        await asyncio.sleep(.05)


def _listener_is_closed(port):
    with socket.socket() as connection:
        connection.settimeout(1)
        return connection.connect_ex(("127.0.0.1", port)) != 0


@pytest.mark.timeout(240)
def test_three_godot_internet_peers_play_ten_hands_after_introduction_stops(tmp_path, application):
    require_crypto()

    async def run():
        baseline = set(asyncio.all_tasks())
        service = IntroductionService("test")
        await service.start()
        peers = []
        try:
            invite, start = tmp_path / "private-invite.txt", tmp_path / "start.signal"
            for name in ("Host", "Guest", "Third"):
                peers.append(InternetDesktopPeer(tmp_path, name, application=application,
                                                 endpoint=service.endpoint, invite=invite, start_signal=start))
            ready = await _wait(peers, "ready_path", "Godot Internet clients did not all become ready")
            assert all(marker == {"network": "internet", "players": 3, "hand_num": 0, "ready": True}
                       for marker in ready), "The setup barrier did not precede the first hand"
            # All peers were admitted through the actual Internet controls.
            # Remove setup before permitting the host's real Start button.
            await service.aclose()
            assert all(value == 0 for value in service.counts().values())
            start.write_text("setup service stopped", encoding="utf-8")
            reports = await _wait(peers, "report_path", "Godot Internet clients did not finish ten hands")
            for report in reports:
                assert report["ok"] is True and report["error"] == "", report
                assert report["network"] == "internet" and report["network_verified"] is True, report
                assert report["hands_settled"] == 10 and report["proofs_verified"] == 3, report
                assert report["face_up_checks"] > 0 and report["opponent_hidden_checks"] > 0, report
                assert set(report["settled_stacks"]) == {str(hand) for hand in range(1, 11)}, report
                assert all(len(stacks) == 3 and sum(stacks) == 300000
                           for stacks in report["settled_stacks"].values()), report
                assert set(report["roster_names"]) == {"Host", "Guest", "Third"}, report
                assert report["commands"].get("check_call", 0) > 0, report
                assert report["commands"].get("next_hand", 0) >= 9, report
                assert not (set(report["commands"]) & {"host_lan", "join_lan"}), report
            assert reports[0]["commands"].get("host_internet") == 1
            assert reports[0]["commands"].get("start_game") == 1
            assert all(report["commands"].get("join_internet") == 1 for report in reports[1:])
            assert {report["own_seat"] for report in reports} == {0, 1, 2}
            assert all(report["settled_stacks"] == reports[0]["settled_stacks"] for report in reports[1:])
            for peer in peers:
                peer.finish()
            sessions = await asyncio.gather(*(asyncio.to_thread(peer.wait_normal_exit) for peer in peers))
            for session in sessions:
                assert session["game_exit_code"] == 0
                assert _listener_is_closed(session["sidecar_port"])
                if os.name == "nt":
                    from tests.test_windows_package import _process_is_running
                    assert not _process_is_running(session["game_pid"])
            assert not any(peer.forced_cleanup for peer in peers)
            (tmp_path / "public-summary.json").write_text(json.dumps({
                "ok": True, "mode": application["mode"], "players": 3,
                "hands": 10, "matching_balances": True, "setup_stopped_before_start": True,
                "normal_exits": [peer.process.returncode for peer in peers],
                "proofs_verified": [report["proofs_verified"] for report in reports],
                "settled_stacks": reports[0]["settled_stacks"],
            }, indent=2) + "\n", encoding="utf-8")
        finally:
            await asyncio.gather(*(asyncio.to_thread(peer.close) for peer in peers))
            await service.aclose()
        await asyncio.sleep(.05)
        assert not [task for task in asyncio.all_tasks() - baseline if not task.done()]

    asyncio.run(run())
