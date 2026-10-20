"""Acceptance against a built Windows preview, rather than an installed checkout.

Set HOLDEM_WINDOWS_EXE to the packaged launcher to opt in. Ordinary source
tests cannot establish that the exported scene, native DLL, Python runtime,
and launcher have actually been delivered together.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time

import pytest


def _packaged_executable():
    configured = os.environ.get("HOLDEM_WINDOWS_EXE")
    if not configured:
        pytest.skip("set HOLDEM_WINDOWS_EXE to test the built Windows preview")
    if os.name != "nt":
        pytest.skip("the packaged preview runs on Windows")
    executable = Path(configured).resolve()
    assert executable.is_file(), f"packaged launcher is missing: {executable}"
    return executable


def _assert_ten_real_hands(summary):
    assert summary["error"] == "", summary
    assert summary["hands_settled"] >= 10, summary
    assert summary["hands_voided"] == 0, summary
    assert summary["settled_hand_numbers"] == list(
        range(1, summary["hands_settled"] + 1)), summary
    assert summary["stack_carry_checks"] >= 9, summary
    assert summary["stack_carry_failures"] == [], summary
    assert summary["refused"] == [], summary
    assert summary["face_up_checks"] > 0, summary
    assert summary["face_up_failures"] == [], summary
    for action in ("check_call", "fold", "raise", "all_in"):
        assert summary["actions"][action] > 0, (action, summary)
    assert summary["lobby_names"] == ["Player (You)", "Bot 1"], summary


def _process_is_running(pid):
    """Query a specific owned process without sending any signal on Windows."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        assert error == 87, f"could not query packaged game PID {pid}: Windows error {error}"
        return False
    try:
        code = wintypes.DWORD()
        assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


def _visible_windows_for(pid):
    import ctypes
    from ctypes import wintypes

    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    found = []

    @callback_type
    def visit(window, _):
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == pid and user.IsWindowVisible(window):
            length = user.GetWindowTextLengthW(window)
            if length:
                title = ctypes.create_unicode_buffer(length + 1)
                user.GetWindowTextW(window, title, len(title))
                found.append((window, title.value))
        return True

    assert user.EnumWindows(visit, 0)
    return found


def _close_owned_window(window, pid):
    """Send the normal close event only after rechecking the window owner."""
    import ctypes
    from ctypes import wintypes

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                 wintypes.LPARAM]
    user.PostMessageW.restype = wintypes.BOOL
    owner = wintypes.DWORD()
    user.GetWindowThreadProcessId(window, ctypes.byref(owner))
    assert owner.value == pid, "the preview window changed owners before close"
    assert user.PostMessageW(window, 0x0010, 0, 0)  # WM_CLOSE


@pytest.fixture
def portable_preview(tmp_path):
    executable = _packaged_executable()
    relocated = tmp_path / "Friends Poker Preview"
    shutil.copytree(executable.parent, relocated)
    working = tmp_path / "unrelated working folder"
    working.mkdir()
    home = tmp_path / "fresh home"
    home.mkdir()
    env = dict(os.environ)
    for name in ("GODOT_BIN", "PYTHONPATH", "PYTHONHOME", "HOLDEM_LIBSODIUM"):
        env.pop(name, None)
    env.update(HOME=str(home), USERPROFILE=str(home),
               HOLDEM_CONFIG_DIR=str(home / "holdem-config"),
               PATH=str(Path(env.get("SYSTEMROOT", "C:/Windows")) / "System32"))
    return relocated / executable.name, working, env, tmp_path / "preview logs"


def _logs(log_dir):
    return "\n".join(path.read_text(encoding="utf-8", errors="replace")
                     for path in sorted(log_dir.rglob("*.log")))


def _assert_listener_closed(port):
    assert 0 < port < 65536
    with socket.socket() as probe:
        probe.settimeout(1.0)
        assert probe.connect_ex(("127.0.0.1", port)) != 0, (
            f"sidecar still accepts connections after launcher exit on {port}")


@pytest.mark.timeout(120)
def test_packaged_preview_plays_ten_hands_after_relocation(portable_preview):
    # Move the entire distributable: a one-folder export may carry assets
    # beside its executable. An unrelated working folder removes the repo
    # and the distribution folder from relative-path resolution.
    copied, working, env, log_dir = portable_preview
    result = subprocess.run(
        [str(copied), "--self-test", "--log-dir", str(log_dir)],
        cwd=working, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=100)
    logs = _logs(log_dir)
    output = result.stdout + "\n" + result.stderr + "\n" + logs
    assert result.returncode == 0, output[-16000:]
    report = json.loads((log_dir / "self-test.json").read_text(encoding="utf-8"))
    assert report["ok"] is True, (report, output)
    assert not report["error"], report
    _assert_ten_real_hands(report["summary"])
    summaries = [json.loads(line.split("E2E_SUMMARY ", 1)[1])
                 for line in output.splitlines() if "E2E_SUMMARY " in line]
    assert summaries, f"packaged client produced no ten-hand evidence:\n{output}"
    assert summaries[-1] == report["summary"]
    assert "Traceback (most recent call last)" not in logs, logs
    session = json.loads((log_dir / "session.json").read_text(encoding="utf-8"))
    assert session["game_exit_code"] == 0, session
    assert not _process_is_running(int(session["game_pid"])), session
    _assert_listener_closed(int(session["sidecar_port"]))


@pytest.mark.timeout(90)
def test_packaged_preview_opens_a_visible_window_and_closes_normally(portable_preview):
    copied, working, env, log_dir = portable_preview
    process = subprocess.Popen(
        [str(copied), "--log-dir", str(log_dir)], cwd=working, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace")
    game_pid, windows = None, []
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            assert process.poll() is None, _logs(log_dir)
            try:
                session = json.loads((log_dir / "session.json").read_text(encoding="utf-8"))
                if session["game_pid"]:
                    game_pid = int(session["game_pid"])
                    windows = _visible_windows_for(game_pid)
                    if windows:
                        break
            except (FileNotFoundError, json.JSONDecodeError):
                pass  # The launcher is still publishing its startup record.
            time.sleep(0.05)
        assert windows, f"the packaged game never opened a visible titled window:\n{_logs(log_dir)}"
        _close_owned_window(windows[0][0], game_pid)
        output, errors = process.communicate(timeout=15)
        assert process.returncode == 0, output + errors + _logs(log_dir)
        assert not _process_is_running(game_pid), "the closed preview game is still running"
        session = json.loads((log_dir / "session.json").read_text(encoding="utf-8"))
        assert session["game_exit_code"] == 0, session
        _assert_listener_closed(int(session["sidecar_port"]))
        (log_dir / "normal-launch.json").write_text(json.dumps({
            "ok": True, "game_pid": game_pid, "title": windows[0][1],
            "game_exit_code": session["game_exit_code"],
            "sidecar_port": session["sidecar_port"],
        }, indent=2) + "\n", encoding="utf-8")
    finally:
        if process.poll() is None:
            # Cleanup is confined to this test's launcher. Its private job
            # owns its game, so closing the launcher also closes that tree.
            process.kill()
            process.communicate(timeout=10)
