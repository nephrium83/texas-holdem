"""Importing the core does nothing.

The core library -- the rules, the crypto and the protocol -- has to embed
in someone else's process. The host decides which files, sockets and
threads exist, so importing a core module must create none of them, and the
core must never reach into the host layer that does.

Two checks, because each sees what the other cannot:

* Each core module is imported in a fresh interpreter, with
  HOLDEM_CONFIG_DIR and the working directory pointing at empty folders.
  Afterwards nothing was written, there is still one thread, and tkinter,
  socket and asyncio are not loaded. A fresh process, because this one has
  already imported whatever the rest of the suite needed; an empty config
  folder, because a module that creates its file on first launch writes
  nothing when the file is already there. Without libsodium, a crypto-backed
  import stops where ristretto loads it: what it did up to there is still
  checked, and then it skips or fails under the crypto-gated suites' policy
  (tests/crypto_gate.py). Any other import error fails.

* No core module imports a host module anywhere in its source. This is read
  from the source rather than observed, so it also sees imports inside
  functions, which do not run at import time: session.py's fallback to the
  global transport is one.

Today's violations are allowlisted below with where each comes from, and
later increments remove the entries. The lists can only shrink: an entry
whose violation has gone fails as well, so the change that removes a
violation removes its entry, and nothing can quietly bring it back.

DELIBERATE-BREAK CONTROLS

``test_control_each_check_sees_its_break``, the import-error controls and
``test_control_the_scan_sees_every_import_form`` run the checks against
small modules written to break them, on every run. A check that stops
seeing its break -- an audit event that changes shape between Python
versions, say -- fails there instead of letting the real modules pass.
"""
from __future__ import annotations

import ast
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

# Every core module that imports a host module today, and from where.
GRAPH_ALLOWLIST = {
    ("holdem.p2p.invite", "holdem.p2p.identity"):
        "invite.py:62, the process-wide key in every invite",
    ("holdem.p2p.join_auth", "holdem.p2p.identity"):
        "join_auth.py:38, the joiner's public key",
    ("holdem.p2p.wire", "holdem.p2p.identity"):
        "wire.py:25, signs and verifies every message",
    ("holdem.p2p.session", "holdem.p2p.transport"):
        "session.py:431, the global transport when none is given",
    ("holdem.p2p.session", "holdem.p2p.device_secret"):
        "session.py:972, the device secret read from disk",
    ("holdem.p2p.session", "holdem.p2p.identity"):
        "session.py:2993, add_local_player",
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

import importlib, json, threading, traceback

failed = None
try:
    importlib.import_module(sys.argv[1])
except BaseException as exc:         # what it did before failing still counts
    where = traceback.extract_tb(exc.__traceback__)[-1]
    failed = {"error": type(exc).__name__,
              "raised_in": [where.filename, where.name],
              "traceback": traceback.format_exc()}
print(json.dumps({
    "writes": writes,
    "threads": threading.active_count(),
    "loaded": [m for m in sys.argv[2:] if m in sys.modules],
    "failed": failed,
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
    report["writes"].extend(
        str(p) for d in (config, cwd) for p in d.rglob("*"))
    return report


def _libsodium_missing(failed: dict) -> bool:
    """Did the import stop because ristretto's loader found no libsodium?

    Only that one raise counts. Any other error, a RuntimeError with the
    same message included, is the import going wrong.
    """
    file, function = failed["raised_in"]
    return (failed["error"] == "RuntimeError"
            and function == "_load_libsodium" and os.path.exists(file)
            and os.path.samefile(file, _source("holdem.p2p.ristretto")))


def _violations(report: dict, tmp_path: Path) -> set:
    found = {f"loads {name}" for name in report["loaded"]}
    if report["threads"] != 1:
        found.add("starts a thread")
    for written in map(Path, report["writes"]):
        if written.is_relative_to(tmp_path):
            written = written.relative_to(tmp_path)
        found.add(f"writes {written.as_posix()}")
    return found


def _source(module: str) -> Path:
    path = REPO.joinpath(*module.split("."))
    return path / "__init__.py" if path.is_dir() else path.with_suffix(".py")


def _imports(source: str, package: str):
    """Yield (name, line) for every name an import in *source* could load.

    ``from holdem.p2p import identity`` yields both ``holdem.p2p`` and
    ``holdem.p2p.identity``, because the second is a module; names that are
    not modules simply match nothing.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parent = package.rsplit(".", node.level - 1)[0]
                base = f"{parent}.{base}" if base else parent
            yield base, node.lineno
            for alias in node.names:
                yield f"{base}.{alias.name}", node.lineno


def _host_imports(module: str) -> dict:
    path = _source(module)
    package = (module if path.name == "__init__.py"
               else module.rpartition(".")[0])
    found = {}
    for name, line in _imports(path.read_text(encoding="utf-8"), package):
        if name in HOST:
            found.setdefault(name, []).append(f"{path.name}:{line}")
    return found


def _check_import(module: str, tmp_path: Path, *path: Path) -> None:
    report = _fresh_import(module, tmp_path, *path)
    found = _violations(report, tmp_path)
    allowed = set(IMPORT_ALLOWLIST.get(module, {}))
    new = sorted(found - allowed)
    # Before anything about how the import ended: a module that writes and
    # then fails has still written.
    assert not new, f"importing {module} must do nothing, but it: {new}"
    failed = report["failed"]
    if failed:
        assert _libsodium_missing(failed), (
            f"importing {module} failed:\n{failed['traceback']}")
        if crypto_gate.crypto_status().available:
            pytest.fail(f"{module} did not find the libsodium this process "
                        f"loads:\n{failed['traceback']}")
        # The rest of the import went unseen, and with it any allowlisted
        # violation, so it is not passed either.
        crypto_gate.require_crypto()       # skips, or fails where required
    gone = sorted(allowed - found)
    assert not gone, (f"{module} no longer does {gone}: remove them from "
                      f"IMPORT_ALLOWLIST so they cannot come back")


@pytest.mark.parametrize("module", sorted(CORE))
def test_importing_a_core_module_does_nothing(module, tmp_path):
    _check_import(module, tmp_path)


@pytest.mark.parametrize("module", sorted(CORE))
def test_the_core_never_imports_the_host_layer(module):
    found = _host_imports(module)
    allowed = {host for core, host in GRAPH_ALLOWLIST if core == module}
    new = {host: where for host, where in found.items() if host not in allowed}
    gone = sorted(allowed - set(found))
    assert not new, f"core module {module} imports the host layer: {new}"
    assert not gone, (f"{module} no longer imports {gone}: remove them from "
                      f"GRAPH_ALLOWLIST so they cannot come back")


def test_every_module_is_core_or_host():
    """A new module is placed on one side before it can escape both checks."""
    on_disk = set()
    for path in (REPO / "holdem").rglob("*.py"):
        parts = path.relative_to(REPO).with_suffix("").parts
        on_disk.add(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    assert not CORE & HOST
    assert not on_disk - CORE - HOST, "unclassified modules"
    assert not (CORE | HOST) - on_disk, "classified modules that do not exist"


def test_allowlists_name_only_core_modules():
    """An entry for a module the checks never visit could never be gone."""
    assert set(IMPORT_ALLOWLIST) <= CORE
    assert {core for core, _ in GRAPH_ALLOWLIST} <= CORE


def _break_module(tmp_path: Path, code: str) -> Path:
    """Write boundary_break.py running *code*; return the folder it is in."""
    lib = tmp_path / "lib"
    elsewhere = tmp_path / "elsewhere"
    lib.mkdir()
    elsewhere.mkdir()
    (lib / "boundary_break.py").write_text(
        "import os, pathlib, threading\n"
        "CONFIG = os.environ['HOLDEM_CONFIG_DIR']\n"
        "NEVER = threading.Event()\n"
        f"ELSEWHERE = {str(elsewhere)!r}\n{code}\n", encoding="utf-8")
    return lib


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
    report = _fresh_import("boundary_break", tmp_path,
                           _break_module(tmp_path, code))
    assert violation in _violations(report, tmp_path)


# The real loader, failing as it does on a machine without the library.
NO_LIBSODIUM = (
    "import ctypes\n"
    "def absent(name, *args, **kwargs): raise OSError(f'{name}: not here')\n"
    "ctypes.CDLL = absent\n"
    "import holdem.p2p.ristretto\n")


@pytest.mark.parametrize("code, missing", [
    (NO_LIBSODIUM, True),
    ("def _load_libsodium():\n"
     "    raise RuntimeError('libsodium with Ristretto255 support could not "
     "be loaded.')\n"
     "_load_libsodium()", False),
], ids=["loader", "lookalike"])
def test_control_only_the_loader_reads_as_libsodium_missing(
        code, missing, tmp_path):
    report = _fresh_import("boundary_break", tmp_path,
                           _break_module(tmp_path, code))
    assert _libsodium_missing(report["failed"]) is missing


def test_control_a_write_before_libsodium_fails_still_fails(tmp_path):
    """Whether or not libsodium loads here, the write is the failure."""
    lib = _break_module(
        tmp_path,
        f"pathlib.Path(ELSEWHERE, 'x').write_text('')\n{NO_LIBSODIUM}")
    with pytest.raises(AssertionError, match="writes elsewhere/x"):
        _check_import("boundary_break", tmp_path, lib)


def test_control_any_other_import_error_fails(tmp_path):
    """Whether or not libsodium loads here, it fails rather than skips."""
    lib = _break_module(tmp_path, "raise RuntimeError('boom')")
    with pytest.raises(AssertionError, match="importing boundary_break failed"):
        _check_import("boundary_break", tmp_path, lib)


def test_control_the_scan_sees_every_import_form():
    source = (
        "import holdem.p2p.transport\n"
        "from holdem.p2p import identity as _id\n"
        "from ..settings import config_dir\n"
        "def later():\n"
        "    from .device_secret import load_or_create\n")
    found = {name for name, _ in _imports(source, "holdem.p2p")} & HOST
    assert found == {"holdem.p2p.transport", "holdem.p2p.identity",
                     "holdem.settings", "holdem.p2p.device_secret"}
