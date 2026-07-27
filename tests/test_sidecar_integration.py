"""Subprocess integration test for holdem.sidecar_launcher + ClientServer.

Launches the sidecar as a real subprocess, reads the SIDECAR_PORT line,
opens a TCP socket, and verifies the JSON protocol through a complete
command round-trip.  No Godot required; this exercises the Python half of
the product path that component tests cannot reach.
"""
from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import time

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STARTUP_TIMEOUT = 10.0   # seconds to wait for SIDECAR_PORT line
RECV_TIMEOUT    = 5.0    # seconds to wait for each message from the sidecar
CHUNK           = 4096


def _start_sidecar(*extra_args: str) -> subprocess.Popen:
    """Launch the sidecar as a subprocess and return the Popen object."""
    return subprocess.Popen(
        [sys.executable, "-m", "holdem.sidecar_launcher", *extra_args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _read_port(proc: subprocess.Popen) -> int:
    """Read the SIDECAR_PORT:<n> line from stdout; fail fast on timeout."""
    deadline = time.monotonic() + STARTUP_TIMEOUT
    buf = ""
    while time.monotonic() < deadline:
        ch = proc.stdout.read(1)
        if not ch:
            break
        buf += ch
        if "\n" in buf:
            line = buf.split("\n", 1)[0].strip()
            if line.startswith("SIDECAR_PORT:"):
                return int(line.split(":", 1)[1])
            buf = buf.split("\n", 1)[1]
    raise TimeoutError(
        f"sidecar did not print SIDECAR_PORT within {STARTUP_TIMEOUT}s. "
        f"stdout so far: {buf!r}"
    )


@contextlib.contextmanager
def _connect(port: int):
    """Open a TCP connection; yield (sock, reader) with proper buffering.

    Using socket.makefile ensures that buffered bytes from one readline()
    call are not lost before the next — critical when hello+snapshot arrive
    in the same TCP segment (always true on loopback).
    """
    deadline = time.monotonic() + 3.0
    while True:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
            break
        except ConnectionRefusedError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)
    sock.settimeout(RECV_TIMEOUT)
    reader = sock.makefile("rb")
    try:
        yield sock, reader
    finally:
        try:
            reader.close()
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass


def _readline(reader) -> dict:
    """Read one newline-terminated JSON message from the buffered reader."""
    line = reader.readline()
    if not line:
        raise EOFError("socket closed before a complete JSON line arrived")
    return json.loads(line)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSidecarHandshake:
    """The sidecar starts, announces its port, and delivers a versioned snapshot."""

    def test_prints_port_on_stdout(self):
        proc = _start_sidecar("--seats", "2")
        try:
            port = _read_port(proc)
            assert isinstance(port, int)
            assert 1 <= port <= 65535
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_hello_then_snapshot(self):
        proc = _start_sidecar("--seats", "2")
        try:
            port = _read_port(proc)
            with _connect(port) as (sock, reader):
                hello = _readline(reader)
                assert hello["type"] == "hello"
                assert hello["protocol"] == 1

                snap = _readline(reader)
                assert snap["type"] == "snapshot"
                assert "phase" in snap
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_snapshot_is_lobby_phase(self):
        proc = _start_sidecar("--seats", "2")
        try:
            port = _read_port(proc)
            with _connect(port) as (sock, reader):
                _readline(reader)              # hello
                snap = _readline(reader)       # snapshot
                assert snap["phase"] == "lobby"
        finally:
            proc.terminate()
            proc.wait(timeout=5)


class TestSidecarCommands:
    """Commands get a command_result response."""

    def test_unknown_command_returns_error(self):
        proc = _start_sidecar("--seats", "2")
        try:
            port = _read_port(proc)
            with _connect(port) as (sock, reader):
                _readline(reader)   # hello
                _readline(reader)   # snapshot
                sock.sendall(
                    json.dumps({"type": "command", "command": "bogus"}).encode()
                    + b"\n"
                )
                result = _readline(reader)
                assert result["type"] == "command_result"
                assert result["ok"] is False
                assert result["command"] == "bogus"
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_command_followed_by_fresh_snapshot(self):
        """A command_result is always followed by a fresh snapshot."""
        proc = _start_sidecar("--seats", "2")
        try:
            port = _read_port(proc)
            with _connect(port) as (sock, reader):
                _readline(reader)   # hello
                _readline(reader)   # snapshot
                sock.sendall(
                    json.dumps({"type": "command", "command": "bogus"}).encode()
                    + b"\n"
                )
                _readline(reader)   # command_result
                snap = _readline(reader)
                assert snap["type"] == "snapshot"
        finally:
            proc.terminate()
            proc.wait(timeout=5)


class TestSidecarArgValidation:
    """Bad arguments cause a non-zero exit with a message on stderr."""

    @pytest.mark.parametrize("args,fragment", [
        (["--seats", "1"],          "minimum 2"),
        (["--seats", "10"],         "maximum 9"),
        (["--small-blind", "0"],    "positive"),
        (["--big-blind", "25"],     "exceed"),     # SB=25 BB=25: not >
        (["--stack", "10"],         "big blind"),  # stack < BB (50)
        (["--port", "99999"],       "65535"),
    ])
    def test_bad_arg_exits_nonzero(self, args, fragment):
        result = subprocess.run(
            [sys.executable, "-m", "holdem.sidecar_launcher", *args],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode != 0
        assert fragment.lower() in (result.stdout + result.stderr).lower()
