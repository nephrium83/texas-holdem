"""Subprocess integration test: Python sidecar + Godot client.

Launches the Python sidecar, then launches Godot headlessly to run
``godot/sidecar/sidecar_test_main.gd``.  Godot connects to the sidecar,
reads hello + snapshot, prints sentinel lines to stdout, and exits 0.

The test is skipped automatically when no Godot binary is found.

CI job: see .github/workflows/ci.yml (godot-sidecar).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths and binary discovery
# ---------------------------------------------------------------------------

_REPO_ROOT     = Path(__file__).resolve().parent.parent
_GODOT_PROJECT = _REPO_ROOT / "godot"
_SIDECAR_GD    = "res://sidecar/sidecar_test_main.gd"

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

# ---------------------------------------------------------------------------
# Helpers (shared with test_sidecar_integration but kept self-contained)
# ---------------------------------------------------------------------------

def _start_sidecar(*extra_args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "holdem.sidecar_launcher", *extra_args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


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

    def test_godot_connects_receives_lobby_snapshot(self):
        """End-to-end: sidecar starts, Godot connects, handshake completes."""
        sidecar = _start_sidecar("--seats", "2")
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
