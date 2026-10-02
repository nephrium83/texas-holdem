"""Subprocess integration tests: Python sidecar + Godot client.

Each test launches the Python sidecar, then launches Godot headlessly:

* ``godot/sidecar/sidecar_test_main.gd`` connects, reads hello + snapshot,
  prints sentinel lines to stdout, and exits 0.
* ``godot/sidecar/main_e2e.gd`` runs the real client scene and plays ten
  hands through its buttons, then prints a JSON summary.

The tests are skipped automatically when no Godot binary is found.

CI job: see .github/workflows/ci.yml (godot-sidecar).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from crypto_gate import require_crypto

# ---------------------------------------------------------------------------
# Paths and binary discovery
# ---------------------------------------------------------------------------

_REPO_ROOT     = Path(__file__).resolve().parent.parent
_GODOT_PROJECT = _REPO_ROOT / "godot"
_SIDECAR_GD    = "res://sidecar/sidecar_test_main.gd"
_E2E_GD        = "res://sidecar/main_e2e.gd"

# Accept $GODOT_BIN env var so CI can pin an exact path; otherwise search PATH.
_GODOT_BIN: str | None = (
    os.environ.get("GODOT_BIN")
    or shutil.which("godot4")
    or shutil.which("godot")
)

# ---------------------------------------------------------------------------
# Timeouts (seconds)
# ---------------------------------------------------------------------------

_SIDECAR_STARTUP = 10.0   # time to see SIDECAR_PORT on sidecar stdout
_GODOT_TIMEOUT   = 30.0   # time to see GODOT_DONE on Godot stdout
# Ten hands take about 9 s locally. Kept inside CI's 60 s per-test timeout
# with room for sidecar startup, so a stuck run fails here with its output
# rather than being killed by pytest-timeout without it.
_E2E_TIMEOUT     = 45.0
# From killing the sidecar to the client's exit: the driver's own deadline is
# 40 s, so an exit within this bound is the driver reacting to the loss.
_LOST_SIDECAR_EXIT = 10.0

# ---------------------------------------------------------------------------
# Helpers (shared with test_sidecar_integration but kept self-contained)
# ---------------------------------------------------------------------------

def _start_sidecar(log_path: Path, *extra_args: str) -> subprocess.Popen:
    """Start the sidecar with its stderr going to *log_path*.

    Never an undrained pipe: nothing reads stderr while a test waits on
    stdout, so once the sidecar has logged more than the pipe buffer holds
    it blocks on its next write, mid-hand. Two seats log almost nothing, but
    three log relay warnings on every hand, and the ten-hand run then stalled
    inside its first hand on Windows. A file cannot fill up that way, and it
    leaves the log to show when a test fails.
    """
    with open(log_path, "w", encoding="utf-8") as log:
        return subprocess.Popen(
            [sys.executable, "-m", "holdem.sidecar_launcher", *extra_args],
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
        )


def _e2e_command(port: int) -> list[str]:
    return [
        _GODOT_BIN, "--headless",
        "--path", str(_GODOT_PROJECT),
        "-s", _E2E_GD,
        "--", f"--sidecar-port={port}", "--hands=10",
    ]


def _tail(path: Path, lines: int = 30) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8",
                                        errors="replace").splitlines()[-lines:])
    except OSError as exc:
        return f"<unreadable: {exc}>"


def _read_port(proc: subprocess.Popen) -> int:
    """Parse SIDECAR_PORT:<n> from the sidecar's stdout."""
    deadline = time.monotonic() + _SIDECAR_STARTUP
    buf = ""
    while time.monotonic() < deadline:
        ch = proc.stdout.read(1)
        if not ch:
            break
        buf += ch
        if "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if line.startswith("SIDECAR_PORT:"):
                return int(line.split(":", 1)[1])
    raise TimeoutError(
        f"sidecar did not print SIDECAR_PORT within {_SIDECAR_STARTUP}s. "
        f"stdout so far: {buf!r}"
    )


def _collect_stdout(proc: subprocess.Popen,
                    sentinel: str,
                    timeout: float) -> list[str]:
    """Read lines from proc stdout until sentinel line or timeout."""
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    buf = ""
    while time.monotonic() < deadline:
        ch = proc.stdout.read(1)
        if not ch:          # process closed its stdout
            break
        buf += ch
        if "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if line:
                lines.append(line)
            if line == sentinel:
                return lines
    return lines


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(_GODOT_BIN is None, reason="godot binary not found in PATH")
class TestGodotSidecarHandshake:
    """Godot connects to the sidecar and receives a valid hello + snapshot."""

    def test_godot_connects_receives_lobby_snapshot(self, tmp_path):
        """End-to-end: sidecar starts, Godot connects, handshake completes."""
        sidecar = _start_sidecar(tmp_path / "sidecar.log", "--seats", "2")
        godot_proc = None
        try:
            port = _read_port(sidecar)

            godot_proc = subprocess.Popen(
                [
                    _GODOT_BIN,
                    "--headless",
                    "--path", str(_GODOT_PROJECT),
                    "-s", _SIDECAR_GD,
                    "--",
                    f"--sidecar-port={port}",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            output = _collect_stdout(godot_proc, sentinel="GODOT_DONE",
                                     timeout=_GODOT_TIMEOUT)
            try:
                godot_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                godot_proc.kill()

            errors = [l for l in output if l.startswith("GODOT_ERROR")]
            assert not errors, f"Godot reported errors: {errors}"
            assert godot_proc.returncode == 0, (
                f"Godot exited {godot_proc.returncode}. "
                f"stdout: {output!r}  "
                f"stderr: {godot_proc.stderr.read()!r}"
            )
            assert "GODOT_CONNECTED" in output, \
                f"GODOT_CONNECTED missing from: {output}"
            assert "GODOT_HELLO:1" in output, \
                f"GODOT_HELLO:1 missing from: {output}"
            assert "GODOT_SNAPSHOT:lobby" in output, \
                f"GODOT_SNAPSHOT:lobby missing from: {output}"
            assert "GODOT_DONE" in output, \
                f"GODOT_DONE missing from: {output}"

        finally:
            if godot_proc and godot_proc.poll() is None:
                godot_proc.terminate()
                try:
                    godot_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    godot_proc.kill()
            sidecar.terminate()
            sidecar.wait(timeout=5)


@pytest.mark.skipif(_GODOT_BIN is None, reason="godot binary not found in PATH")
class TestGodotClientPlays:
    """The real client scene plays real hands, driven through its buttons."""

    def test_ten_hands_through_the_real_buttons(self, tmp_path):
        """main_e2e.gd instantiates main.tscn against a live sidecar and
        presses Start, Check/Call, Fold, Raise (on the slider), All In and
        Next Hand -- only ever a visible, enabled button, and only once the
        previous command was answered. Every hand is a real mental-poker
        deal, so this needs libsodium; the sidecar logs at INFO into a file.

        That INFO run does not reproduce the stall an undrained stderr pipe
        causes: two seats write about 120 bytes of stderr over ten hands at
        any level, and the run passes with the pipe put back (three seats is
        where it stalls; see _start_sidecar). The regression control for the
        redirect is the ``sidecar.stderr is None`` assertion.

        The deep stack keeps nine hands of calling from busting the player
        before the all-in, which comes last because it can end the match.
        """
        require_crypto()
        sidecar_log = tmp_path / "sidecar.log"
        godot_log = tmp_path / "godot.stderr.log"
        sidecar = _start_sidecar(sidecar_log, "--seats", "2", "--stack",
                                 "100000", "--seed", "7", "--log-level", "INFO")
        godot_proc = None
        try:
            assert sidecar.stderr is None, "sidecar stderr is an undrained pipe"
            port = _read_port(sidecar)
            with open(godot_log, "w", encoding="utf-8") as err:
                godot_proc = subprocess.Popen(
                    _e2e_command(port),
                    stdout=subprocess.PIPE,
                    stderr=err,
                    text=True, encoding="utf-8", errors="replace",
                )
            try:
                output, _ = godot_proc.communicate(timeout=_E2E_TIMEOUT)
            except subprocess.TimeoutExpired:
                godot_proc.kill()
                output, _ = godot_proc.communicate()
                pytest.fail(f"the client did not finish within {_E2E_TIMEOUT}s."
                            f"\nstdout:\n{output}\nsidecar log:\n"
                            f"{_tail(sidecar_log)}")

            lines = output.splitlines()
            summaries = [line for line in lines
                         if line.startswith("E2E_SUMMARY ")]
            context = (f"Godot exited {godot_proc.returncode}.\nstdout tail:\n"
                       + "\n".join(lines[-40:])
                       + f"\nGodot stderr:\n{_tail(godot_log)}"
                       + f"\nsidecar log:\n{_tail(sidecar_log)}")
            assert summaries, f"no E2E_SUMMARY line. {context}"
            summary = json.loads(summaries[-1].split(" ", 1)[1])

            assert summary["error"] == "", context
            assert godot_proc.returncode == 0, context
            assert summary["hands_settled"] >= 10, context
            assert summary["hands_voided"] == 0, context
            assert summary["settled_hand_numbers"] == list(
                range(1, summary["hands_settled"] + 1)
            ), context
            assert summary["stack_carry_checks"] >= 9, context
            assert summary["stack_carry_failures"] == [], context
            assert summary["refused"] == [], context
            for action in ("check_call", "fold", "raise", "all_in"):
                assert summary["actions"][action] >= 1, \
                    f"{action} was never applied. {context}"
            assert summary["face_up_checks"] > 0, context
            assert summary["face_up_failures"] == [], context
            assert summary["lobby_names"] == ["Player (You)", "Bot 1"], context
        finally:
            if godot_proc and godot_proc.poll() is None:
                godot_proc.kill()
                godot_proc.wait(timeout=5)
            sidecar.terminate()
            sidecar.wait(timeout=5)

    def test_a_sidecar_lost_mid_run_ends_the_run_with_that_error(self, tmp_path):
        """However a run ends, the driver reports and quits. A lost sidecar
        is noticed in a deferred signal, between frames, and the driver used
        to mark itself finished there and then return from every later frame
        before reporting: Godot printed no summary and never exited, so a
        crashed sidecar showed up only as the 45 s timeout, and a harness
        that never killed Godot leaked a process that spun forever.

        The sidecar is killed once the first hand has settled; the client
        must exit 1 within seconds with the loss as its summary's error.
        """
        require_crypto()
        sidecar_log = tmp_path / "sidecar.log"
        godot_out = tmp_path / "godot.stdout.log"
        godot_err = tmp_path / "godot.stderr.log"
        sidecar = _start_sidecar(sidecar_log, "--seats", "2", "--stack",
                                 "100000", "--seed", "7")
        godot_proc = None
        try:
            port = _read_port(sidecar)
            with open(godot_out, "w", encoding="utf-8") as out, \
                    open(godot_err, "w", encoding="utf-8") as err:
                godot_proc = subprocess.Popen(_e2e_command(port),
                                              stdout=out, stderr=err)

            def output() -> str:
                return godot_out.read_text(encoding="utf-8", errors="replace")

            deadline = time.monotonic() + _E2E_TIMEOUT
            while "E2E_HAND_SETTLED" not in output():
                if godot_proc.poll() is not None or time.monotonic() > deadline:
                    pytest.fail(f"no hand settled before the kill.\nstdout:\n"
                                f"{output()}\nsidecar log:\n{_tail(sidecar_log)}")
                time.sleep(0.05)
            sidecar.kill()
            sidecar.wait(timeout=5)

            try:
                godot_proc.wait(timeout=_LOST_SIDECAR_EXIT)
            except subprocess.TimeoutExpired:
                pytest.fail(f"the client was still running {_LOST_SIDECAR_EXIT}s "
                            f"after the sidecar died.\nstdout:\n{output()}")

            summaries = [line for line in output().splitlines()
                         if line.startswith("E2E_SUMMARY ")]
            context = (f"Godot exited {godot_proc.returncode}.\nstdout:\n"
                       f"{output()}\nGodot stderr:\n{_tail(godot_err)}")
            assert summaries, f"no E2E_SUMMARY line. {context}"
            summary = json.loads(summaries[-1].split(" ", 1)[1])
            assert summary["error"] == "sidecar disconnected", context
            assert summary["hands_settled"] >= 1, context
            assert godot_proc.returncode == 1, context
        finally:
            if godot_proc and godot_proc.poll() is None:
                godot_proc.kill()
                godot_proc.wait(timeout=5)
            if sidecar.poll() is None:
                sidecar.kill()
                sidecar.wait(timeout=5)
