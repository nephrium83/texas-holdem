"""External acceptance of one immutable desktop ZIP through production invites.

The private invite is delivered by an authenticated ephemeral X25519 courier.
GitHub receives public keys or ciphertext, never a usable game capability.
This helper does not send game actions; the packaged Godot UI driver does.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit
import zipfile

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def read(path):
    raw = Path(path).read_bytes()
    if len(raw) > 256 * 1024:
        raise ValueError("Acceptance file exceeds its bound")
    return json.loads(raw)


def write(path, value):
    Path(path).write_bytes(canonical(value) + b"\n")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def public(key):
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def new_key(path):
    key = X25519PrivateKey.generate()
    Path(path).write_bytes(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                           serialization.NoEncryption()))
    return key


def load_key(path):
    return X25519PrivateKey.from_private_bytes(Path(path).read_bytes())


def courier_context(*, run_id, run_attempt, nonce, source_commit, package_sha256, host_public, guest_public):
    for value, count in ((nonce, 64), (source_commit, 40), (package_sha256, 64),
                         (host_public, 64), (guest_public, 64)):
        if not isinstance(value, str) or re.fullmatch("[0-9a-f]{%d}" % count, value) is None:
            raise ValueError("Invalid courier context")
    if any(re.fullmatch("[1-9][0-9]{0,19}", str(v)) is None for v in (run_id, run_attempt)):
        raise ValueError("Invalid courier run")
    return {"purpose": "poker-desktop-invite-courier-v1", "run_id": str(run_id),
            "run_attempt": str(run_attempt), "nonce": nonce, "source_commit": source_commit,
            "package_sha256": package_sha256, "host_public": host_public, "guest_public": guest_public}


def courier_key(key, remote_public, context):
    shared = key.exchange(X25519PublicKey.from_public_bytes(bytes.fromhex(remote_public)))
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=hashlib.sha256(canonical(context)).digest(),
                info=b"poker-desktop-invite-courier-v1").derive(shared)


def validate_delivery(value):
    if not isinstance(value, dict) or set(value) != {"invite", "package_url"}:
        raise ValueError("Invalid private delivery")
    invite, url = value["invite"], value["package_url"]
    if not isinstance(invite, str) or not invite.startswith("poker3:") or len(invite) > 4096:
        raise ValueError("Invalid private invite")
    if not isinstance(url, str) or len(url) > 8192:
        raise ValueError("Invalid package delivery URL")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "release-assets.githubusercontent.com"
            or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment
            or not parsed.path.startswith("/github-production-release-asset/")):
        raise ValueError("Package delivery must use the official signed release asset origin")
    return value


def seal_delivery(delivery, key, context):
    validate_delivery(delivery)
    if public(key) != context["host_public"]:
        raise ValueError("Host courier identity differs")
    nonce = os.urandom(12)
    cipher = AESGCM(courier_key(key, context["guest_public"], context)).encrypt(
        nonce, canonical(delivery), canonical(context))
    return {"context": context, "nonce": nonce.hex(), "ciphertext": base64.b64encode(cipher).decode("ascii")}


def open_delivery(envelope, key, context):
    if (not isinstance(envelope, dict) or set(envelope) != {"context", "nonce", "ciphertext"}
            or envelope["context"] != context or public(key) != context["guest_public"]
            or re.fullmatch("[0-9a-f]{24}", envelope["nonce"]) is None
            or not isinstance(envelope["ciphertext"], str) or len(envelope["ciphertext"]) > 24000):
        raise ValueError("Invalid encrypted invite envelope")
    plain = AESGCM(courier_key(key, context["host_public"], context)).decrypt(
        bytes.fromhex(envelope["nonce"]), base64.b64decode(envelope["ciphertext"], validate=True), canonical(context))
    return validate_delivery(json.loads(plain))


def extract_package(archive, destination, source_commit, package_sha256):
    if digest(archive) != package_sha256:
        raise ValueError("Desktop ZIP differs from the selected hash")
    destination = Path(destination).resolve()
    destination.mkdir(exist_ok=False)
    with zipfile.ZipFile(archive) as zipped:
        if len(zipped.infolist()) > 2500 or sum(p.file_size for p in zipped.infolist()) > 1024**3:
            raise ValueError("Unexpected desktop ZIP bounds")
        for entry in zipped.infolist():
            target = (destination / entry.filename).resolve()
            if not target.is_relative_to(destination) or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Unsafe desktop archive member")
        zipped.extractall(destination)
    # build_windows.py archives the application contents at the ZIP root.
    app = destination
    manifest = read(app / "build-manifest.json")
    actual = {p.relative_to(app).as_posix(): digest(p) for p in app.rglob("*")
              if p.is_file() and p != app / "build-manifest.json"}
    if actual != manifest["files"] or manifest["source_commit"] != source_commit or manifest["source_dirty"]:
        raise ValueError("Desktop source or complete file inventory differs")
    if "Poker.exe" not in actual or manifest["package_versions"].get("websockets") != "16.1.1":
        raise ValueError("Desktop launcher or setup client missing")
    return app


class DesktopRun:
    """Own only a newly launched packaged test; never search or kill services."""
    def __init__(self, app, root, role):
        self.root = Path(root).resolve()
        self.root.mkdir(exist_ok=False)
        self.role = role
        self.report = self.root / "report.json"
        self.ready = self.root / "ready.json"
        self.invite = self.root / "private-invite.txt"
        self.start = self.root / "start.signal"
        self.finish = self.root / "finish.signal"
        self.abort = self.root / "abort.signal"
        config = {"network": "internet", "role": role, "nickname": "Host" if role == "host" else "Guest",
                  "target_hands": 10, "expected_players": 2, "deadline_seconds": 360,
                  "report_path": str(self.report), "ready_signal_path": str(self.ready),
                  "start_signal_path": str(self.start), "finish_signal_path": str(self.finish),
                  "abort_signal_path": str(self.abort),
                  "invite_write_path" if role == "host" else "invite_read_path": str(self.invite)}
        write(self.root / "private-config.json", config)
        environment = dict(os.environ, HOLDEM_CONFIG_DIR=str(self.root / "config"))
        for name in ("PYTHONPATH", "PYTHONHOME", "GODOT_BIN", "HOLDEM_LIBSODIUM", "HOLDEM_INTRODUCTION_URL"):
            environment.pop(name, None)
        windows = Path(environment.get("SystemRoot", "C:/Windows"))
        environment["PATH"] = os.pathsep.join(map(str, (windows / "System32", windows)))
        with (self.root / "private-launcher.log").open("wb") as log:
            self.process = subprocess.Popen([str(Path(app).resolve() / "Poker.exe"),
                "--internet-self-test-config", str(self.root / "private-config.json"),
                "--log-dir", str(self.root / "logs")], cwd=self.root, env=environment,
                stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def wait_file(self, path, timeout=300):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return path
            if self.process.poll() is not None:
                raise RuntimeError("Owned desktop exited before its acceptance marker")
            time.sleep(.1)
        raise TimeoutError("Desktop acceptance marker timed out")

    def complete(self):
        report = read(self.wait_file(self.report))
        receipt = validate_report(report, self.role)
        self.finish.write_text("complete", encoding="ascii")
        code = self.process.wait(timeout=30)
        session = read(self.root / "logs/session.json")
        check = read(self.root / "logs/internet-self-test.json")
        if code != 0 or session["game_exit_code"] != 0 or check.get("ok") is not True:
            raise RuntimeError("Desktop or Godot did not exit successfully")
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(("127.0.0.1", session["sidecar_port"])) == 0:
                raise RuntimeError("Desktop sidecar listener remains open")
        return {**receipt, "launcher_exit_code": code, "godot_exit_code": session["game_exit_code"],
                "sidecar_closed": True}

    def close(self):
        if self.process.poll() is None:
            self.abort.write_text("owned test shutdown", encoding="ascii")
            self.finish.write_text("complete", encoding="ascii")
            # No process termination. A remaining test must be reported.
            self.process.wait(timeout=30)


def validate_report(report, role):
    balances = report.get("settled_stacks", {})
    commands = report.get("commands", {})
    if (report.get("ok") is not True or report.get("error") != "" or report.get("network") != "internet"
            or report.get("network_verified") is not True or report.get("hands_settled") != 10
            or report.get("proofs_verified") != 2 or report.get("face_up_checks", 0) <= 0
            or report.get("opponent_hidden_checks", 0) <= 0
            or set(report.get("roster_names", [])) != {"Host", "Guest"}
            or set(balances) != set(map(str, range(1, 11)))
            or any(not isinstance(stacks, list) or len(stacks) != 2 or
                       any(type(n) is not int or n < 0 for n in stacks) or sum(stacks) != 200000
                   for stacks in balances.values())
            or commands.get("check_call", 0) <= 0 or commands.get("next_hand", 0) < 9
            or set(commands) & {"host_lan", "join_lan"}
            or commands.get("host_internet" if role == "host" else "join_internet") != 1
            or (role == "host" and commands.get("start_game") != 1)):
        raise ValueError("Desktop did not pass ten real Internet UI hands")
    return {"role": role, "network": "internet", "hands": 10, "proofs_verified": 2,
            "own_seat": report["own_seat"], "settled_stacks": balances,
            "face_up_checks": report["face_up_checks"], "opponent_hidden_checks": report["opponent_hidden_checks"]}


def guest_prepare(root):
    root.mkdir(exist_ok=False)
    (root / "private").mkdir()
    (root / "public").mkdir()
    key = new_key(root / "private/courier.key")
    write(root / "public/ready.json", {"guest_public": public(key)})


def guest_run(root):
    key = load_key(root / "private/courier.key")
    context = courier_context(run_id=os.environ["GITHUB_RUN_ID"], run_attempt=os.environ["GITHUB_RUN_ATTEMPT"],
        nonce=os.environ["ICE_NONCE"], source_commit=os.environ["ICE_SOURCE_REF"],
        package_sha256=os.environ["ICE_DESKTOP_PACKAGE_SHA256"], host_public=os.environ["ICE_HOST_ENCRYPTION_PUBLIC"],
        guest_public=public(key))
    gist = os.environ["ICE_MAILBOX_GIST_ID"]
    if re.fullmatch("[0-9a-f]{32}", gist) is None:
        raise ValueError("Invalid encrypted mailbox ID")
    deadline = time.monotonic() + 180
    delivery = None
    while time.monotonic() < deadline:
        request = urllib.request.Request("https://api.github.com/gists/" + gist,
                                         headers={"User-Agent": "poker-desktop-acceptance", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise ValueError("Mailbox exceeds bound")
        file = json.loads(raw).get("files", {}).get("invite.json")
        if file and not file.get("truncated"):
            delivery = open_delivery(json.loads(file["content"]), key, context)
            break
        time.sleep(3)
    if delivery is None:
        raise TimeoutError("Encrypted invite did not arrive")
    # The signed asset URL is encrypted along with the invite. The runner
    # gets temporary read access to this one ZIP, without a write token or
    # access to unpublished releases. Bound the response before extracting.
    with urllib.request.urlopen(delivery["package_url"], timeout=60) as response, (root / "Poker.zip").open("xb") as output:
        total = 0
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > 512 * 1024 * 1024:
                raise ValueError("Desktop ZIP download exceeds bound")
            output.write(chunk)
    app = extract_package(root / "Poker.zip", root / "private/package", context["source_commit"], context["package_sha256"])
    desktop = DesktopRun(app, root / "private/desktop", "join")
    try:
        desktop.invite.write_text(delivery["invite"], encoding="ascii")
        receipt = desktop.complete()
        write(root / "public/result.json", {"context": context, "receipt": receipt})
    finally:
        desktop.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "run"))
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.phase == "prepare":
            guest_prepare(args.root)
        else:
            guest_run(args.root)
    except Exception as error:
        # Network/game exceptions and private logs never enter public output.
        if (args.root / "private").exists():
            (args.root / "private/failure.txt").write_text(repr(error), encoding="utf-8")
        print("Desktop acceptance failed; private diagnostics retained.")
        return 1
    print("Desktop acceptance phase passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
