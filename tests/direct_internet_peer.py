"""Isolated desktop peers with an explicitly fake Internet port-mapping edge.

Only the router lease is replaced. Each subprocess owns the production
DesktopTable, LanTable, signed TCP transport, admission handshake and crypto
deal. The parent's private pipe controls the desktop; it never forwards peer
messages. This helper cannot contact a router or mutate a real port mapping.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

from lan_peer import REPO_ROOT, _redacted, wait_until


_RESULT_PREFIX = "DIRECT_PEER_RESULT:"


class DirectInternetPeer:
    def __init__(self, root: Path, name: str, *, mapping_failure=False):
        self.name = name
        self.root = root / name
        self.root.mkdir(parents=True)
        self.log_path = self.root / "direct-peer.log"
        self._snapshot = None
        self._closed = False
        self._replies = queue.Queue()
        env = dict(os.environ, HOLDEM_CONFIG_DIR=str(self.root / "config"))
        with self.log_path.open("w", encoding="utf-8") as log:
            self.process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--worker", name,
                 *(["--mapping-failure"] if mapping_failure else [])],
                cwd=REPO_ROOT, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=log, text=True,
            )

        def read_replies():
            for line in self.process.stdout:
                if line.startswith(_RESULT_PREFIX):
                    self._replies.put(json.loads(line[len(_RESULT_PREFIX):]))
            self._replies.put(None)

        threading.Thread(target=read_replies, daemon=True).start()
        try:
            ready = self._receive("startup", timeout=20)
            assert ready == {"ready": True}, self.context("startup failed")
            self.wait(lambda snap: snap["lobby"]["mode"] == "menu",
                      description="desktop menu")
        except BaseException:
            self.close()
            raise

    def _receive(self, description, *, timeout=35):
        try:
            result = self._replies.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(self.context(f"timed out: {description}")) from None
        assert result is not None, self.context(f"process exited: {description}")
        assert "worker_error" not in result, self.context(
            f"worker error: {_redacted(result)!r}")
        return result

    def _request(self, operation, **payload):
        assert self.process.poll() is None, self.context(operation)
        self.process.stdin.write(json.dumps(dict(operation=operation, **payload)) + "\n")
        self.process.stdin.flush()
        return self._receive(operation)

    @property
    def snapshot(self):
        self._snapshot = self._request("snapshot")
        return self._snapshot

    def context(self, description):
        return (f"{self.name}: {description}; process={self.process.poll()}; "
                f"snapshot={_redacted(self._snapshot)!r}; runtime log={self.log_path}")

    def wait(self, predicate, *, timeout=35, description="snapshot condition"):
        def matches():
            return predicate(self.snapshot)

        try:
            wait_until(matches, timeout=timeout, description=description)
        except AssertionError as exc:
            raise AssertionError(self.context(str(exc))) from None
        return self._snapshot

    def command(self, command, payload=None):
        result = self._request("command", command=command, payload=payload or {})
        self.snapshot
        return result

    def mappings(self):
        return self._request("mappings")

    def lose_mapping(self, index=0, reason="The test mapping could not be renewed"):
        return self._request("lose_mapping", index=index, reason=reason)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self.process.poll() is None:
                self._request("shutdown")
                self.process.wait(timeout=10)
        finally:
            if self.process.poll() is None:
                # Scope is only this helper's owned test process.
                self.process.terminate()
                self.process.wait(timeout=8)
            for stream in (self.process.stdin, self.process.stdout):
                if stream is not None:
                    stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _worker(name, *, mapping_failure):
    # Running this file directly puts tests/ rather than the repository on
    # sys.path. This is a subprocess-local import path, not a product setting.
    sys.path.insert(0, str(REPO_ROOT))
    from holdem import lan_session, port_mapping
    from holdem.desktop_table import DesktopTable

    def forbid_real_mapping(*_args, **_kwargs):
        raise AssertionError("This isolated acceptance test must never access a real router")

    async def no_stun_discovery():
        return None

    # Even if factory wiring regresses, fail before reaching a real router.
    # Legacy TCP startup's unused UDP discovery is also kept offline here.
    port_mapping.open_tcp_mapping = forbid_real_mapping
    lan_session.transport._resolve_stun = no_stun_discovery

    attempts = []
    leases = []

    class FakeLease:
        def __init__(self, local_ip, local_port, on_lost):
            self.external_address = ("127.0.0.1", local_port)
            self.local_address = (local_ip, local_port)
            self.on_lost = on_lost
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    def fake_mapping_factory(local_ip, local_port, on_lost):
        # The injected mapping makes the invite reach the real TCP listener
        # on loopback. No discovery function or UPnP request is called.
        assert isinstance(local_ip, str) and local_ip
        assert type(local_port) is int and 0 < local_port < 65536
        attempts.append(dict(local_ip=local_ip, local_port=local_port))
        if mapping_failure:
            raise RuntimeError("The test router refuses automatic port mapping")
        lease = FakeLease(local_ip, local_port, on_lost)
        leases.append(lease)
        return lease

    original_table = lan_session.LanTable

    class InjectedTable(original_table):
        def __init__(self, nickname, settings, on_change=None, **kwargs):
            super().__init__(nickname, settings, on_change,
                             mapping_factory=fake_mapping_factory, **kwargs)

    lan_session.LanTable = InjectedTable
    table = DesktopTable(seats=3, nickname=name, seed=7,
                         settings={"sb": 25, "bb": 50, "stack": 1000,
                                   "structure": "No-Limit"},
                         on_change=lambda: None)

    def reply(value):
        print(_RESULT_PREFIX + json.dumps(value), flush=True)

    reply({"ready": True})
    try:
        for line in sys.stdin:
            request = json.loads(line)
            operation = request["operation"]
            try:
                if operation == "snapshot":
                    reply(table.snapshot())
                elif operation == "command":
                    command = request["command"]
                    try:
                        reply(table.apply_command(command, request["payload"]))
                    except Exception as exc:
                        # Mirror the renderer server's visible refusal path.
                        reply(dict(type="command_result", command=command,
                                   ok=False, verdict="error", error=str(exc)))
                elif operation == "mappings":
                    reply(dict(attempts=attempts,
                               leases=[dict(local_address=lease.local_address,
                                            external_address=lease.external_address,
                                            close_calls=lease.close_calls)
                                       for lease in leases]))
                elif operation == "lose_mapping":
                    lease = leases[request["index"]]
                    # Renewal failures originate off the desktop command
                    # thread. Exercise that real concurrency boundary.
                    thread = threading.Thread(target=lease.on_lost,
                                              args=(request["reason"],), daemon=True)
                    thread.start()
                    thread.join(timeout=2)
                    reply({"triggered": True, "finished": not thread.is_alive()})
                elif operation == "shutdown":
                    table.close()
                    reply({"closed": True})
                    break
                else:
                    raise ValueError("Unknown private test operation")
            except Exception as exc:
                reply({"worker_error": f"{type(exc).__name__}: {exc}"})
    finally:
        table.close()


if __name__ == "__main__":
    assert len(sys.argv) >= 3 and sys.argv[1] == "--worker"
    _worker(sys.argv[2], mapping_failure="--mapping-failure" in sys.argv[3:])
