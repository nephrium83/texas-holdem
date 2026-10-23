"""Paired renderer diagnostics fail before play when their setup is invalid.

These tests never request Internet hosting or a router mapping. The mismatch
case joins an ordinary LAN room through the production renderer buttons.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from crypto_gate import require_crypto
from lan_peer import REPO_ROOT, SidecarPeer


GODOT_BIN = (os.environ.get("GODOT_BIN") or shutil.which("godot4")
             or shutil.which("godot"))
pytestmark = pytest.mark.skipif(GODOT_BIN is None,
                                reason="godot binary not found in PATH")


def _run_driver(root: Path, config: dict, *, port=None):
    path = root / "peer-config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    command = [GODOT_BIN, "--headless", "--path", str(REPO_ROOT / "godot"),
               "-s", "res://sidecar/lan_peer_e2e.gd", "--",
               f"--lan-peer-config={path}"]
    if port is not None:
        command.append(f"--sidecar-port={port}")
    result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=20)
    report = Path(config["report_path"])
    assert report.is_file(), f"Driver did not report: {result.stdout}\n{result.stderr}"
    return result, json.loads(report.read_text(encoding="utf-8"))


def test_invalid_network_fails_before_connecting_to_any_sidecar(tmp_path):
    result, report = _run_driver(tmp_path, {
        "role": "host", "network": "unsupported",
        "report_path": str(tmp_path / "report.json"),
    })
    assert result.returncode == 1
    assert report["ok"] is False
    assert report["error"] == "Paired test network must be lan or internet"
    assert report["commands"] == {}
    assert report["network_checks"] == report["hands_settled"] == 0


def test_internet_guest_rejects_lan_metadata_before_ready_or_play(tmp_path):
    require_crypto()
    with ExitStack() as stack:
        host = stack.enter_context(SidecarPeer(tmp_path, "Host"))
        guest = stack.enter_context(SidecarPeer(tmp_path, "Guest"))
        assert host.command("host_lan", {"nickname": "Host"})["ok"]
        snapshot = host.wait(lambda value: bool(value["lobby"].get("invite")),
                             description="ordinary LAN invite")
        invite_path = tmp_path / "private-invite.txt"
        invite_path.write_text(snapshot["lobby"]["invite"], encoding="utf-8")
        result, report = _run_driver(tmp_path, {
            "role": "join", "nickname": "Guest", "network": "internet",
            "invite_read_path": str(invite_path), "target_hands": 1,
            "deadline_seconds": 15, "report_path": str(tmp_path / "report.json"),
        }, port=guest.port)
        assert result.returncode == 1
        assert report["ok"] is False
        assert report["error"] == "The table network differs from the paired test configuration"
        assert report["network"] == "internet"
        assert report["commands"] == {"join_lan": 1}
        assert report["network_checks"] == report["hands_settled"] == 0
        assert "invite" not in report and "hole" not in report
