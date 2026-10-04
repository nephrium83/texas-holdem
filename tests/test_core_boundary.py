"""Importing the core does nothing.

The core library -- the rules, the crypto and the protocol -- has to embed
in someone else's process. The host decides which files, sockets and
threads exist, so importing a core module must create none of them.

Each core module is imported in a fresh interpreter, with HOLDEM_CONFIG_DIR
and the working directory pointing at empty folders. Afterwards nothing was
written, there is still one thread, and tkinter, socket and asyncio are not
loaded. A fresh process, because this one has already imported whatever the
rest of the suite needed; an empty config folder, because a module that
creates its file on first launch writes nothing when the file is already
there.

Today's violations are allowlisted below with where each comes from, and
later increments remove the entries. The list can only shrink: an entry
whose violation has gone fails as well, so the change that removes a
violation removes its entry, and nothing can quietly bring it back.

DELIBERATE-BREAK CONTROLS

``test_control_each_check_sees_its_break`` runs the checks against small
modules written to break them, on every run. A check that stops seeing its
break -- an audit event that changes shape between Python versions, say --
fails there instead of letting the real modules pass.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import crypto_gate

# The core: the rules, the crypto and the protocol, which is what a host
# imports. The two packages count as core because importing anything inside
# them runs their __init__ first.
CORE = frozenset({
    # rules
    "holdem", "holdem.engine", "holdem.player_info", "holdem.contract",
    # crypto
    "holdem.p2p.ristretto", "holdem.p2p.elgamal", "holdem.p2p.pedersen",
    "holdem.p2p.dleq", "holdem.p2p.keygen_pop", "holdem.p2p.shuffle_mp",
    "holdem.p2p.shuffle_proof", "holdem.p2p.bg_challenge",
    "holdem.p2p.bg_hadamard", "holdem.p2p.bg_product",
    "holdem.p2p.bg_shuffle", "holdem.p2p.bg_svp", "holdem.p2p.bg_wire",
    "holdem.p2p.bg_witness", "holdem.p2p.bg_zero", "holdem.p2p.deck_audit",
    "holdem.p2p.deal_map", "holdem.p2p.mental_deal",
    # protocol
    "holdem.p2p", "holdem.p2p.session", "holdem.p2p.replica_table",
    "holdem.p2p.mental_deal_driver", "holdem.p2p.timeout",
    "holdem.p2p.events", "holdem.p2p.admission", "holdem.p2p.invite",
    "holdem.p2p.wire", "holdem.p2p.join_auth",
    "holdem.p2p.inmemory_transport", "holdem.client_view",
})

# The host layer: sockets and threads, key and secret files, client
# settings, the local JSON server and the sidecar, the Tk GUI, audio, hand
# history and notes. settings.py stays here whole until its table rules move
# into the core; session_stats feeds the Tk HUD; tcp_transport is headed for
# the tests, its only users.
HOST = frozenset({
    "holdem.__main__", "holdem.audio", "holdem.client_server", "holdem.gui",
    "holdem.hand_history", "holdem.notes", "holdem.onboarding",
    "holdem.session_stats", "holdem.settings", "holdem.sidecar_launcher",
    "holdem.p2p.transport", "holdem.p2p.stun", "holdem.p2p.dispatch",
    "holdem.p2p.identity", "holdem.p2p.device_secret",
    "holdem.p2p.tcp_transport",
})

FORBIDDEN_MODULES = ("tkinter", "socket", "asyncio")

_IDENTITY = "through identity, which creates it at import (identity.py:83-85)"

# What a fresh import of each core module does today that it must not. A
# write names its file, so a second file from one of these is still new.
IMPORT_ALLOWLIST = {
    "holdem.p2p.invite": {
        "writes config/identity.json": "invite.py:62, " + _IDENTITY,
        "loads socket":
            "invite.py:59, for inet_aton/inet_ntoa at invite.py:145-218",
    },
    "holdem.p2p.join_auth": {
        "writes config/identity.json": "join_auth.py:38-39, " + _IDENTITY,
    },
    "holdem.p2p.wire": {
        "writes config/identity.json": "wire.py:25, " + _IDENTITY,
    },
}

# Runs in the fresh interpreter. The audit hook goes in before the import,
# so it sees every write the import attempts wherever it points; the empty
# folders only catch writes that land in them.
PROBE = r"""
import os, sys

writes = []
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
WRITE_EVENTS = {"os.mkdir", "os.rename", "os.remove", "os.rmdir",
                "os.truncate", "os.symlink", "os.link"}

def audit(event, args):
    if event == "open":
        path, mode, flags = args
        # io.open reports its mode string; os.open has only its flags.
        writing = (any(c in mode for c in "wax+") if mode
                   else flags & WRITE_FLAGS)
        if writing:
            writes.append(str(path))
    elif event == "os.mkdir" and os.path.isdir(args[0]):
        pass                     # exist_ok on a folder already there
    elif event in WRITE_EVENTS:
        writes.append(str(args[0]))

sys.addaudithook(audit)

import importlib, json, threading

try:
    importlib.import_module(sys.argv[1])
except RuntimeError as exc:          # libsodium did not load
    print(json.dumps({"unloadable": str(exc)}))
    sys.exit()
print(json.dumps({
    "writes": writes,
    "threads": threading.active_count(),
    "loaded": [m for m in sys.argv[2:] if m in sys.modules],
}))
"""


def _fresh_import(module: str, tmp_path: Path, *path: Path) -> dict:
    """Import *module* alone in a new interpreter and report what it did."""
    config = tmp_path / "config"
    cwd = tmp_path / "cwd"
    config.mkdir()
    cwd.mkdir()
    env = dict(os.environ, HOLDEM_CONFIG_DIR=str(config))
    # This checkout, whether or not it is the copy pip installed.
    env["PYTHONPATH"] = os.pathsep.join(
        str(p) for p in (*path, REPO, env.get("PYTHONPATH")) if p)
    # -B: a bytecode cache would be the interpreter's write, not the module's.
    proc = subprocess.run(
        [sys.executable, "-B", "-c", PROBE, module, *FORBIDDEN_MODULES],
        cwd=cwd, env=env, capture_output=True, text=True, errors="replace",
        timeout=60)
    assert proc.returncode == 0, f"importing {module} crashed:\n{proc.stderr}"
    report = json.loads(proc.stdout.splitlines()[-1])
    # Whatever reached the empty folders, however it was written.
    report.setdefault("writes", []).extend(
        str(p) for d in (config, cwd) for p in d.rglob("*"))
    return report


def _violations(report: dict, tmp_path: Path) -> set:
    found = {f"loads {name}" for name in report["loaded"]}
    if report["threads"] != 1:
        found.add("starts a thread")
    for written in map(Path, report["writes"]):
        if written.is_relative_to(tmp_path):
            written = written.relative_to(tmp_path)
        found.add(f"writes {written.as_posix()}")
    return found


@pytest.mark.parametrize("module", sorted(CORE))
def test_importing_a_core_module_does_nothing(module, tmp_path):
    report = _fresh_import(module, tmp_path)
    if "unloadable" in report:
        if crypto_gate.crypto_status().available:
            pytest.fail(f"{module} did not import: {report['unloadable']}")
        crypto_gate.require_crypto()       # skips, or fails where required
    found = _violations(report, tmp_path)
    allowed = set(IMPORT_ALLOWLIST.get(module, {}))
    new = sorted(found - allowed)
    gone = sorted(allowed - found)
    assert not new, f"importing {module} must do nothing, but it: {new}"
    assert not gone, (f"{module} no longer does {gone}: remove them from "
                      f"IMPORT_ALLOWLIST so they cannot come back")


def test_every_module_is_core_or_host():
    """A new module is placed on one side before it can escape the check."""
    on_disk = set()
    for path in (REPO / "holdem").rglob("*.py"):
        parts = path.relative_to(REPO).with_suffix("").parts
        on_disk.add(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    assert not CORE & HOST
    assert not on_disk - CORE - HOST, "unclassified modules"
    assert not (CORE | HOST) - on_disk, "classified modules that do not exist"


def test_allowlist_names_only_core_modules():
    """An entry for a module the check never visits could never be gone."""
    assert set(IMPORT_ALLOWLIST) <= CORE


@pytest.mark.parametrize("code, violation", [
    ("open(os.path.join(CONFIG, 'x'), 'w').close()", "writes config/x"),
    ("pathlib.Path(ELSEWHERE, 'x').write_text('')", "writes elsewhere/x"),
    ("os.close(os.open(os.path.join(ELSEWHERE, 'x'), os.O_CREAT))",
     "writes elsewhere/x"),
    ("os.mkdir(os.path.join(ELSEWHERE, 'x'))", "writes elsewhere/x"),
    ("threading.Thread(target=NEVER.wait, daemon=True).start()",
     "starts a thread"),
    ("import socket", "loads socket"),
], ids=["config-folder", "write-text-elsewhere", "os-open-elsewhere",
        "mkdir-elsewhere", "thread", "socket"])
def test_control_each_check_sees_its_break(code, violation, tmp_path):
    """ELSEWHERE is outside both empty folders, so only the hook sees it."""
    lib = tmp_path / "lib"
    elsewhere = tmp_path / "elsewhere"
    lib.mkdir()
    elsewhere.mkdir()
    (lib / "boundary_break.py").write_text(
        "import os, pathlib, threading\n"
        "CONFIG = os.environ['HOLDEM_CONFIG_DIR']\n"
        "NEVER = threading.Event()\n"
        f"ELSEWHERE = {str(elsewhere)!r}\n{code}\n", encoding="utf-8")
    report = _fresh_import("boundary_break", tmp_path, lib)
    assert violation in _violations(report, tmp_path)
