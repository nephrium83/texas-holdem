"""Private process/files harness for actual Godot Internet controls.

It neither opens peer connections nor drives poker actions. Each desktop
launcher owns its sidecar and Godot child; the Godot driver presses the UI.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


class InternetDesktopPeer:
    def __init__(self, root, name, *, application, endpoint, invite, start_signal):
        self.root = Path(root) / name
        self.root.mkdir()
        self.name = name
        self.logs = self.root / "logs"
        self.report_path = self.root / "report.json"
        self.ready_path = self.root / "ready.json"
        self.finish_path = self.root / "finish.signal"
        self.abort_path = self.root / "abort.signal"
        self.process = None
        self.forced_cleanup = False
        config = {"network": "internet", "role": "host" if name == "Host" else "join",
                  "nickname": name, "target_hands": 10, "expected_players": 3,
                  "deadline_seconds": 180, "report_path": str(self.report_path),
                  "ready_signal_path": str(self.ready_path), "start_signal_path": str(start_signal),
                  "finish_signal_path": str(self.finish_path), "abort_signal_path": str(self.abort_path),
                  "test_service_endpoint": endpoint,
                  "invite_write_path" if name == "Host" else "invite_read_path": str(invite)}
        config_path = self.root / "private-config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        environment = dict(os.environ, HOLDEM_CONFIG_DIR=str(self.root / "config"))
        environment.pop("HOLDEM_INTRODUCTION_URL", None)
        if application["mode"] == "source":
            command = [sys.executable, "-m", "holdem.desktop_launcher",
                       "--godot", str(application["godot"]), "--pck", str(application["pck"]),
                       "--libsodium", str(application["sodium"])]
        else:
            command = [str(application["executable"])]
            environment.pop("PYTHONPATH", None)
            environment.pop("PYTHONHOME", None)
            environment.pop("HOLDEM_LIBSODIUM", None)
            windows = Path(environment.get("SystemRoot", "C:/Windows"))
            environment["PATH"] = os.pathsep.join(str(p) for p in (windows / "System32", windows))
        command += ["--internet-self-test-config", str(config_path), "--log-dir", str(self.logs)]
        with (self.root / "private-launcher.log").open("w", encoding="utf-8") as log:
            self.process = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=log, stderr=log)

    def read(self, path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def check_running(self):
        if self.process.poll() is not None:
            raise AssertionError(f"{self.name} desktop exited before completing its UI check (code {self.process.returncode})")

    def finish(self):
        self.finish_path.write_text("complete", encoding="utf-8")

    def wait_normal_exit(self):
        code = self.process.wait(timeout=25)
        assert code == 0, f"{self.name} desktop did not exit normally (code {code})"
        result = self.read(self.logs / "internet-self-test.json")
        assert result and result.get("ok") is True, f"{self.name} desktop did not validate its UI receipt"
        return self.read(self.logs / "session.json")

    def close(self):
        if self.process is None or self.process.poll() is not None:
            return
        self.abort_path.write_text("owned test shutdown", encoding="utf-8")
        self.finish()
        try:
            self.process.wait(timeout=25)
        except subprocess.TimeoutExpired:
            # This exact child belongs solely to this ephemeral test. Success
            # requires normal exit; forced cleanup is recorded as a failure.
            self.forced_cleanup = True
            self.process.terminate()
            self.process.wait(timeout=15)
