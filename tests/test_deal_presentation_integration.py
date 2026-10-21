"""The delivered desktop path presents a real new hand before bots can act.

The older ten-hand acceptance starts a bare Session, which deliberately keeps
its synchronous test driver. This test chooses Local bots and Start game in
the real menu against DesktopTable, the path used by the normal launcher.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

import pytest

from crypto_gate import require_crypto


_ROOT = Path(__file__).resolve().parents[1]
_GODOT = (os.environ.get("GODOT_BIN") or shutil.which("godot4")
          or shutil.which("godot"))


def _tail(path):
    return path.read_text(encoding="utf-8", errors="replace")[-18000:]


def _port(process, log):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        match = re.search(r"SIDECAR_PORT:(\d+)", _tail(log))
        if match:
            return int(match.group(1))
        assert process.poll() is None, _tail(log)
        time.sleep(0.03)
    raise AssertionError(f"The desktop sidecar did not become ready:\n{_tail(log)}")


@pytest.mark.skipif(_GODOT is None, reason="godot binary not found in PATH")
@pytest.mark.timeout(100)
def test_normal_desktop_menu_shuffles_deals_and_commits_all_in_once(tmp_path):
    require_crypto()
    sidecar_log = tmp_path / "desktop-sidecar.log"
    game_log = tmp_path / "presentation.log"
    report_path = tmp_path / "presentation.json"
    with sidecar_log.open("w", encoding="utf-8") as log:
        sidecar = subprocess.Popen([
            sys.executable, "-m", "holdem.sidecar_launcher",
            "--desktop-lobby", "--seats", "2", "--stack", "1000",
            "--seed", "7", "--log-level", "INFO",
        ], cwd=_ROOT, stdout=log, stderr=subprocess.STDOUT)
    try:
        port = _port(sidecar, sidecar_log)
        with game_log.open("w", encoding="utf-8") as log:
            game = subprocess.run([
                _GODOT, "--headless", "--path", str(_ROOT / "godot"),
                "-s", "res://sidecar/presentation_e2e.gd", "--",
                f"--sidecar-port={port}",
                f"--presentation-report={report_path}",
            ], cwd=_ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=85)
        output = _tail(game_log) + "\n" + _tail(sidecar_log)
        assert game.returncode == 0, output
        summary = json.loads(report_path.read_text(encoding="utf-8"))
        assert summary["ok"] is True and summary["error"] == "", summary
        for field in ("menu_fresh_checks", "opening_preaction_checks",
                      "shuffle_frames", "deal_frames", "shuffle_motion_checks",
                      "deal_motion_checks", "moving_back_checks",
                      "masked_control_checks", "private_card_checks",
                      "next_hand_presentations", "bot_reaction_checks"):
            assert summary[field] > 0, (field, summary)
        assert summary["presentations_completed"] >= 2, summary
        assert summary["all_in_clicks"] == 1 and summary["all_in_applied"], summary
        assert summary["cancelled_during_deal"] is True, summary
        assert summary["returned_to_menu"] is True, summary
        assert summary["menu_quiet_ms"] >= 3200, summary
        assert summary["refused"] == [], summary
        assert "SCRIPT ERROR:" not in output, output
        assert "Traceback (most recent call last)" not in output, output
    finally:
        # This process belongs only to this test and serves no live game.
        if sidecar.poll() is None:
            sidecar.terminate()
        sidecar.wait(timeout=10)
