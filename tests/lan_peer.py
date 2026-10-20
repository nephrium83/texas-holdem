"""Bounded client for the shipped desktop sidecar in LAN acceptance tests.

Each instance is a separate Python process with its own device-secret
directory. This helper speaks only the public renderer socket protocol; it
does not create Sessions, fake admission, route peer traffic or deal cards.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time


REPO_ROOT = Path(__file__).resolve().parents[1]


def _redacted(value):
    """Keep room capabilities out of pytest assertion and timeout output."""
    if isinstance(value, dict):
        return {key: "<redacted>" if key == "invite" else _redacted(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    return value


class SidecarPeer:
    def __init__(self, root: Path, name: str, *, seats: int = 3,
                 stack: int = 1000, seed: int | None = None):
        self.name = name
        self.root = root / name
        self.root.mkdir(parents=True)
        self.log_path = self.root / "sidecar.log"
        self._condition = threading.Condition()
        self._snapshot = None
        self._messages: list[tuple[int, dict]] = []
        self._sequence = 0
        self._error = None
        self._socket = None
        self._reader = None
        self._closed = False
        self.process = None
        self.port = None
        env = dict(os.environ, HOLDEM_CONFIG_DIR=str(self.root / "config"))
        with self.log_path.open("w", encoding="utf-8") as log:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "holdem.sidecar_launcher",
                 "--desktop-lobby", "--nickname", name,
                 "--seats", str(seats), "--stack", str(stack),
                 "--small-blind", "5", "--big-blind", "10",
                 *(["--seed", str(seed)] if seed is not None else [])],
                cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE,
                stderr=log, text=True,
            )
        try:
            stdout = queue.Queue()

            def collect_stdout():
                for line in self.process.stdout:
                    stdout.put(line.strip())
                stdout.put(None)

            threading.Thread(target=collect_stdout, daemon=True).start()
            deadline = time.monotonic() + 15
            while self.port is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(self.context("sidecar startup timed out"))
                try:
                    line = stdout.get(timeout=remaining)
                except queue.Empty:
                    raise AssertionError(self.context("sidecar startup timed out"))
                if line is None:
                    raise AssertionError(self.context("sidecar exited before ready"))
                if line.startswith("SIDECAR_PORT:"):
                    self.port = int(line.split(":", 1)[1])
            self._socket = socket.create_connection(("127.0.0.1", self.port), 5)
            self._socket.settimeout(None)
            self._reader = self._socket.makefile("rb")
            threading.Thread(target=self._read_messages, daemon=True).start()
            self.wait(lambda snap: snap.get("lobby", {}).get("mode") == "menu",
                      description="desktop main menu")
        except BaseException:
            self.close()
            raise

    def _read_messages(self):
        try:
            for line in self._reader:
                message = json.loads(line)
                with self._condition:
                    self._sequence += 1
                    if message.get("type") == "snapshot":
                        self._snapshot = message
                    self._messages.append((self._sequence, message))
                    self._messages = self._messages[-256:]
                    self._condition.notify_all()
            if not self._closed:
                raise EOFError("renderer socket closed")
        except Exception as exc:
            with self._condition:
                self._error = f"{type(exc).__name__}: {exc}"
                self._condition.notify_all()

    @property
    def snapshot(self) -> dict:
        with self._condition:
            return copy.deepcopy(self._snapshot)

    def context(self, description: str) -> str:
        # Runtime logs are deliberately not dumped: a failure in production
        # logging must not turn an assertion into publication of an invite.
        return (f"{self.name}: {description}; process={self.process.poll()}; "
                f"reader={self._error}; snapshot={_redacted(self._snapshot)!r}; "
                f"runtime log={self.log_path}")

    def wait(self, predicate, *, timeout: float = 35,
             description: str = "snapshot condition") -> dict:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if self._snapshot is not None and predicate(self._snapshot):
                    return copy.deepcopy(self._snapshot)
                if self._error is not None:
                    raise AssertionError(self.context(description))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(self.context(f"timed out: {description}"))
                self._condition.wait(remaining)

    def command(self, command: str, payload: dict | None = None,
                *, timeout: float = 35) -> dict:
        with self._condition:
            sent_after = self._sequence
        self._socket.sendall(json.dumps({
            "type": "command", "command": command, "payload": payload or {},
        }).encode() + b"\n")
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                result = next(((seq, message) for seq, message in self._messages
                               if seq > sent_after
                               and message.get("type") == "command_result"
                               and message.get("command") == command), None)
                if result is not None:
                    result_seq, message = result
                    # Await the guaranteed post-result snapshot as well as
                    # the acknowledgement, rather than inspect stale state.
                    if any(seq > result_seq and item.get("type") == "snapshot"
                           for seq, item in self._messages):
                        return copy.deepcopy(message)
                if self._error is not None:
                    raise AssertionError(self.context(f"command {command}"))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(self.context(f"command {command} timed out"))
                self._condition.wait(remaining)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._socket.close()
        if self._reader is not None:
            self._reader.close()
        if self.process is not None and self.process.poll() is None:
            # Only the subprocess created by this helper is owned here.
            self.process.terminate()
            self.process.wait(timeout=8)
        if self.process is not None and self.process.stdout is not None:
            self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def wait_until(predicate, *, timeout: float = 35, description: str):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out: {description}")
        time.sleep(0.025)


def listener_closed(address: str, port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.3)
        return probe.connect_ex((address, port)) != 0
