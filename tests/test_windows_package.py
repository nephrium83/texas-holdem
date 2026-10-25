"""Acceptance against a built Windows preview, rather than an installed checkout.

Set HOLDEM_WINDOWS_EXE to the packaged launcher to opt in. Ordinary source
tests cannot establish that the exported scene, native DLL, Python runtime,
and launcher have actually been delivered together.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
SPEC = importlib.util.spec_from_file_location("windows_preview_build", ROOT / "tools" / "build_windows.py")
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


def _manifest(application, **overrides):
    path = application / "build-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.update(overrides)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def _rehash_inventory(application):
    files = {str(p.relative_to(application)).replace("\\", "/"): build.sha256(p)
             for p in application.rglob("*") if p.is_file() and p.name != "build-manifest.json"}
    return _manifest(application, files=files, native_files_sha256={
        name: digest for name, digest in files.items() if Path(name).suffix in (".exe", ".dll", ".pyd")})


def _application(tmp_path):
    application = tmp_path / "Poker"
    paths = {
        "Poker.exe": b"launcher", "_internal/assets/PokerRuntime.exe": b"Godot runtime",
        "_internal/assets/poker.pck": b"game resources", "_internal/native/libsodium.dll": b"official sodium",
        "_internal/av/minimal.pyd": b"minimal PyAV binding", "licenses/libsodium/LICENSE.txt": b"ISC notice",
        "licenses/libsodium/SOURCE.txt": b"source pin", "licenses/libsodium/provenance.json": b"native provenance",
    }
    for relative, contents in paths.items():
        path = application / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    digest = lambda name: build.sha256(application / name)
    sodium = {"version": "1.0.22", "dll_sha256": digest("_internal/native/libsodium.dll"),
              "license_sha256": digest("licenses/libsodium/LICENSE.txt"),
              "attribution_sha256": digest("licenses/libsodium/SOURCE.txt"),
              "provenance_sha256": digest("licenses/libsodium/provenance.json")}
    manifest = {"godot_sha256": digest("_internal/assets/PokerRuntime.exe"),
                "pck_sha256": digest("_internal/assets/poker.pck"), "libsodium": sodium,
                "libsodium_sha256": sodium["dll_sha256"], "package_versions": {"websockets": "16.1.1"},
                "license_coverage": {"minimal_runtime": {"native_files_sha256": {
                    "av/minimal.pyd": digest("_internal/av/minimal.pyd")}}}}
    (application / "build-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _rehash_inventory(application)
    build.verify_manifest(application)
    return application


@pytest.mark.parametrize("mutation", ["remove", "replace", "extra", "native-inventory"])
def test_desktop_manifest_refuses_incomplete_or_modified_closure(tmp_path, mutation):
    application = _application(tmp_path)
    library = application / "_internal" / "av" / "minimal.pyd"
    if mutation == "remove":
        library.unlink()
    elif mutation == "replace":
        library.write_bytes(b"other binding")
    elif mutation == "extra":
        (application / "_internal" / "unknown.dll").write_bytes(b"unreviewed DLL")
    else:
        _manifest(application, native_files_sha256={})
    with pytest.raises(RuntimeError, match="hashes/coverage"):
        build.verify_manifest(application)


@pytest.mark.parametrize(("relative", "error"), [
    ("_internal/assets/PokerRuntime.exe", "Godot runtime/resources"),
    ("_internal/assets/poker.pck", "Godot runtime/resources"),
    ("_internal/native/libsodium.dll", "libsodium coverage/hash"),
    ("licenses/libsodium/LICENSE.txt", "libsodium coverage/hash"),
    ("_internal/av/minimal.pyd", "minimal PyAV native files"),
])
def test_generic_inventory_cannot_override_reviewed_game_native_or_notice_inputs(tmp_path, relative, error):
    application = _application(tmp_path)
    (application / relative).write_bytes(b"replacement")
    _rehash_inventory(application)
    with pytest.raises(RuntimeError, match=error):
        build.verify_manifest(application)


def test_desktop_requires_exact_websocket_client_and_launcher(tmp_path):
    application = _application(tmp_path)
    _manifest(application, package_versions={"websockets": "17.0"})
    with pytest.raises(RuntimeError, match="reviewed version"):
        build.verify_manifest(application)
    (application / "Poker.exe").unlink()
    _rehash_inventory(application)
    with pytest.raises(RuntimeError, match="missing its launcher"):
        build.verify_manifest(application)


def test_desktop_rejects_stock_pyav_before_creating_an_artifact(tmp_path, monkeypatch):
    from types import SimpleNamespace
    godot, sodium, result = (tmp_path / name for name in ("Godot.exe", "libsodium.dll", "sodium.json"))
    for path in (godot, sodium, result):
        path.write_bytes(b"fixture")
    monkeypatch.setattr(build.sys, "platform", "win32")
    monkeypatch.setattr(build, "verify_sodium_result", lambda _path: {"dll": str(sodium), "dll_sha256": build.sha256(sodium)})
    monkeypatch.setattr(build, "runtime_distributions", lambda: {"av": SimpleNamespace(version="17.1.0")})
    monkeypatch.setattr(build.ice_build, "source_snapshot", lambda: {})
    output = tmp_path / "output"
    with pytest.raises(RuntimeError, match="minimal PyAV"):
        build.main(["--godot", str(godot), "--sodium-result", str(result), "--output-dir", str(output)])
    assert not output.exists()


def test_desktop_result_file_cannot_overwrite_an_existing_reviewed_result(tmp_path, monkeypatch):
    result = tmp_path / "reviewed.json"
    result.write_text("old result", encoding="utf-8")
    monkeypatch.setattr(build.sys, "platform", "win32")
    with pytest.raises(SystemExit) as error:
        build.main(["--godot", "unused.exe", "--sodium-result", "unused.json", "--result-json", str(result)])
    assert error.value.code == 2 and result.read_text(encoding="utf-8") == "old result"


def test_desktop_runtime_requires_the_complete_matching_hand_receipt():
    summary = {"schema": "direct-poker-self-test-v1", "status": "passed", "hands": 10,
               "matching_balances": True, "proofs_verified": 2, "deal_policy": "bayer-groth-v1",
               "peer_closed": True, "owned_tasks_pending": 0, "callback_workers_alive": 0}
    report = {"ok": True, "summary": summary}
    assert build.validate_internet_self_test(report) == report
    with pytest.raises(RuntimeError, match="runtime check failed"):
        build.validate_internet_self_test({"ok": False, "summary": summary})
    for field, value in (("hands", 9), ("matching_balances", False), ("proofs_verified", 0),
                         ("owned_tasks_pending", 1), ("callback_workers_alive", 1)):
        with pytest.raises(RuntimeError, match="matching hands/proofs and cleanup"):
            build.validate_internet_self_test({"ok": True, "summary": {**summary, field: value}})


def test_desktop_runtime_closure_requires_pinned_setup_library(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(build.ice_build, "runtime_distributions", lambda: {"aiortc": object()})
    monkeypatch.setattr(build.importlib.metadata, "distribution", lambda _name: SimpleNamespace(version="16.1.1"))
    assert build.runtime_distributions()["websockets"].version == "16.1.1"
    monkeypatch.setattr(build.importlib.metadata, "distribution", lambda _name: SimpleNamespace(version="17.0"))
    with pytest.raises(RuntimeError, match="websockets==16.1.1"):
        build.runtime_distributions()


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
    for name in ("GODOT_BIN", "PYTHONPATH", "PYTHONHOME", "HOLDEM_LIBSODIUM", "HOLDEM_INTRODUCTION_URL"):
        env.pop(name, None)
    env.update(HOME=str(home), USERPROFILE=str(home),
               HOLDEM_CONFIG_DIR=str(home / "holdem-config"),
               PATH=str(Path(env.get("SYSTEMROOT", "C:/Windows")) / "System32"))
    build.verify_manifest(relocated)
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


@pytest.mark.timeout(190)
def test_packaged_internet_runtime_plays_ten_real_hands_after_relocation(portable_preview):
    copied, working, env, log_dir = portable_preview
    result = subprocess.run(
        [str(copied), "--internet-runtime-self-test", "--log-dir", str(log_dir)],
        cwd=working, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180)
    output = result.stdout + "\n" + result.stderr + "\n" + _logs(log_dir)
    assert result.returncode == 0, output[-16000:]
    report = json.loads((log_dir / "internet-runtime-self-test.json").read_text(encoding="utf-8"))
    build.validate_internet_self_test(report)
    assert not (log_dir / "session.json").exists(), "Runtime verification unexpectedly launched Godot"


@pytest.mark.timeout(40)
def test_packaged_desktop_never_falls_back_when_bundled_sodium_is_missing(portable_preview):
    copied, working, env, log_dir = portable_preview
    external = _packaged_executable().parent / "_internal" / "native" / "libsodium.dll"
    assert external.is_file()
    (copied.parent / "_internal" / "native" / "libsodium.dll").unlink()
    env["HOLDEM_LIBSODIUM"] = str(external)
    result = subprocess.run(
        [str(copied), "--internet-runtime-self-test", "--log-dir", str(log_dir)],
        cwd=working, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30)
    assert result.returncode != 0, "The portable game silently used an external libsodium DLL"
    report = json.loads((log_dir / "internet-runtime-self-test.json").read_text(encoding="utf-8"))
    assert report["ok"] is False and report["summary"] is None


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
