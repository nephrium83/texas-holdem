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


def _command(sock, reader, command: str, payload: dict | None = None):
    """Send a command; return (command_result, the snapshot that follows it).

    Deliberately matched by identity, not by position. Starting a table runs
    a whole hand inside one round-trip, and every state change along the way
    queues an unprompted snapshot (section 5). Reading a fixed two lines
    would hand back whichever message happened to be next -- a stale
    snapshot read as a command_result -- so this skips to the result for
    THIS command and then takes the next snapshot after it.
    """
    sock.sendall(json.dumps({"type": "command", "command": command,
                             "payload": payload or {}}).encode() + b"\n")
    while True:
        msg = _readline(reader)
        if msg.get("type") == "command_result" and msg.get("command") == command:
            result = msg
            break
    while True:
        msg = _readline(reader)
        if msg.get("type") == "snapshot":
            return result, msg


def _await_snapshot(reader, predicate, what: str, limit: int = 200):
    """Read snapshots until one satisfies *predicate*.

    Bounded by message count as well as by the socket timeout, so a
    sidecar that keeps pushing snapshots without ever reaching the wanted
    state fails with a useful message instead of hanging.
    """
    last = None
    for _ in range(limit):
        msg = _readline(reader)
        if msg.get("type") != "snapshot":
            continue
        last = msg
        if predicate(msg):
            return msg
    raise AssertionError(
        f"never observed {what} within {limit} messages; last snapshot: "
        f"phase={(last or {}).get('phase')!r} "
        f"voided={(last or {}).get('voided')!r}")


class TestClientCanReachTheMentalDeal:
    """The load-bearing reachability test.

    Everything here runs through the shipped surface: a real sidecar
    subprocess, a real localhost socket, and the real protocol. No harness
    shortcut, no direct call into Session.

    This exists because the mental-poker deal had NO reachable production
    caller. MentalDealDriver is constructed only in Session.begin_hand,
    reached only from _begin_p2p_hand, reached only from start_p2p_hand and
    next_p2p_hand -- and start_p2p_hand's only caller in holdem/ was
    _deal_first_hand, which run() never called and only tests used. The
    crypto was built, tested, and unreachable. If this test ever goes red by
    landing back in the lobby, that regression has returned.
    """

    def test_start_game_leaves_the_lobby_and_deals_real_cards(self):
        proc = _start_sidecar("--seats", "3")
        try:
            port = _read_port(proc)
            with _connect(port) as (sock, reader):
                _readline(reader)                       # hello
                lobby = _readline(reader)
                assert lobby["phase"] == "lobby", "did not start in lobby"

                # A real deal runs inside this round-trip; makefile() reads
                # share the socket timeout, so raising it here covers both.
                sock.settimeout(60.0)
                result, snap = _command(sock, reader, "start_game")

                assert result["ok"] is True, result
                assert result["verdict"] == "started", result
                assert snap["phase"] != "lobby", \
                    "start_game returned started but the table stayed in lobby"

                # A real deal ran: this seat holds two real cards.
                hole = snap["you"].get("hole")
                assert hole and len(hole) == 2, \
                    f"no hole cards dealt -- deal did not run: {hole!r}"
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_the_local_replica_reaches_live_play(self):
        """The deal does not merely run -- it hands off to real betting.

        A deal that completed but left the replica inert would still show
        hole cards, so this asserts the seat is actually in the hand: either
        it is this seat's turn, or the hand already progressed past it.

        Deliberately NOT asserting on deal_progress. That field reports
        lifecycle position and nothing else -- it is a pure function of
        phase, which is exactly why its old name (`verification`, with a
        "verified" state) was removed. Asserting on it would be a phase
        check dressed as a proof check, and it would also be a race: with
        three seats the hand often stops at this seat's decision and never
        reaches settled. The Bayer-Groth assertion belongs on proof
        evidence, not on a display label.
        """
        proc = _start_sidecar("--seats", "3")
        try:
            port = _read_port(proc)
            with _connect(port) as (sock, reader):
                _readline(reader)
                _readline(reader)
                sock.settimeout(60.0)
                _result, snap = _command(sock, reader, "start_game")

                assert snap["phase"] in ("betting", "settled"), \
                    f"replica never reached live play: phase={snap['phase']!r}"
                turn_state = snap.get("turn", {}).get("state")
                assert turn_state != "lobby", \
                    f"turn state stayed in lobby: {turn_state!r}"
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_the_shipped_table_declares_and_runs_bayer_groth(self):
        """The mandate, asserted on the shipped socket.

        Two halves, and both are needed. deal_policy proves the policy
        reached the client, which is what lets a front end state the
        table's security level instead of implying it. proofs_verified
        proves the engine OBEYED it: it counts only proofs that came back
        valid from bg_shuffle.verify, so a table that declares Bayer-Groth
        while dealing without proofs reports zero.

        The second half exists because the first is not enough, and I
        established that the expensive way. This test originally used
        elapsed wall-clock as the obedience check -- a proofless deal being
        ~15x cheaper -- and deleting `prevention=prevention` from the
        driver, which silently downgrades the deal while leaving every
        policy string byte-identical, did not fire it: socket and
        full-hand overhead alone clear any floor loose enough to be
        stable. A timing proxy could not tell "ran no proofs" from "ran on
        a fast machine". This counts the thing itself.
        """
        proc = _start_sidecar("--seats", "3")
        try:
            port = _read_port(proc)
            with _connect(port) as (sock, reader):
                _readline(reader)
                lobby = _readline(reader)
                assert lobby["deal_policy"] is None, \
                    "lobby claimed a policy before a table was accepted"

                sock.settimeout(60.0)
                _result, snap = _command(sock, reader, "start_game")

                assert snap["deal_policy"] == "bayer-groth-v1", \
                    f"table did not declare Bayer-Groth: {snap['deal_policy']!r}"
                # Three seats shuffle in turn, and this seat verifies every
                # round it did not author.
                assert snap["proofs_verified"] >= 2, (
                    f"table declared bayer-groth-v1 but this seat verified "
                    f"{snap['proofs_verified']} shuffle proofs -- the policy "
                    f"string arrived without the proofs behind it")
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_second_start_game_does_not_restart_the_table(self):
        """start_game is not idempotent-by-accident: a duplicate must report
        already_started rather than silently re-dealing a live table."""
        proc = _start_sidecar("--seats", "2")
        try:
            port = _read_port(proc)
            with _connect(port) as (sock, reader):
                _readline(reader)
                _readline(reader)
                sock.settimeout(60.0)
                first, _ = _command(sock, reader, "start_game")
                assert first["verdict"] == "started"

                second, _ = _command(sock, reader, "start_game")
                assert second["verdict"] == "already_started", second
                assert second["ok"] is False
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


class TestTheProductDrivesItsOwnTimeouts:
    """P2: Session.check_deadlines had NO production caller.

    The deterministic timeout machinery -- deadlines, proposals, the void
    path -- was complete, tested and unreachable, so a hand that stalled
    stalled forever. Exactly the shape of the MentalDealDriver defect this
    suite's other class exists for, one subsystem over.

    These tests never call check_deadlines. That is the point: the claim
    is not "timeouts work", which was already true, but "the shipped
    product drives them".
    """

    def test_a_stalled_deal_fails_closed_through_the_production_ticker(self):
        """The load-bearing one, and PR #34's M9 reproduced end to end.

        A seat stops answering deal traffic. start_game still reports
        started -- that verdict is a synchronous table-start result and
        cannot divine a hand's future -- the client sees "dealing", and
        then the production ticker drives the existing deadline to a void.
        Nothing here touches the timeout API directly.
        """
        proc = _start_sidecar(
            "--seats", "3",
            "--test-stall-seat", "2",
            "--test-deadline-scale", "0.02",     # 30s deal deadline -> 0.6s
            "--test-tick-interval", "0.05",
        )
        try:
            port = _read_port(proc)
            with _connect(port) as (sock, reader):
                _readline(reader)                          # hello
                _readline(reader)                          # lobby snapshot

                sock.settimeout(30.0)
                result, snap = _command(sock, reader, "start_game")

                # The table did start; only its hand is doomed.
                assert result["verdict"] == "started", result
                assert snap["phase"] == "dealing", (
                    f"expected a stalled deal, got phase={snap['phase']!r}")
                assert snap["voided"] is False

                # ...and now the product's own ticker resolves it, with no
                # help from this test.
                voided = _await_snapshot(
                    reader,
                    lambda s: s.get("voided") is True,
                    "the stalled hand to void")

                assert voided["void_reason"] == "deal timeout: deal_shuffle", \
                    voided["void_reason"]
                assert voided["turn"]["state"] == "voided"
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_a_healthy_table_is_not_voided_by_the_ticker(self):
        """The ticker must not be a hand-killer. Same accelerated
        deadlines, no stalled seat: the hand plays normally."""
        proc = _start_sidecar(
            "--seats", "3",
            "--test-deadline-scale", "0.05",
            "--test-tick-interval", "0.05",
        )
        try:
            port = _read_port(proc)
            with _connect(port) as (sock, reader):
                _readline(reader)
                _readline(reader)
                sock.settimeout(30.0)
                _result, snap = _command(sock, reader, "start_game")

                assert snap["phase"] in ("betting", "settled"), snap["phase"]
                assert snap["voided"] is False
                assert len(snap["you"].get("hole") or []) == 2
        finally:
            proc.terminate()
            proc.wait(timeout=5)
