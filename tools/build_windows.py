"""Build a portable Windows preview without Godot export templates.

The installed Godot executable is the runtime for an exported resource pack.
Every invocation uses fresh build/output folders and leaves prior artifacts
alone. No Python, Godot editor installation, or native-library path is needed
on the player's machine.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def run(command, *, cwd=ROOT, env=None, log=None):
    print("Running:", subprocess.list2cmdline([str(part) for part in command]), flush=True)
    result = subprocess.run(
        [str(part) for part in command], cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", check=False,
    )
    if log:
        Path(log).write_text(result.stdout, encoding="utf-8")
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}); log: {log or 'above'}")
    return result.stdout


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_licenses(destination, godot, libsodium, work):
    destination.mkdir()
    shutil.copy2(ROOT / "LICENSE", destination / "Texas-Holdem-LICENSE.txt")
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError(f"Python license not found: {python_license}")
    shutil.copy2(python_license, destination / "Python-LICENSE.txt")
    for package in ("cryptography", "cffi", "pycparser", "pyinstaller", "miniupnpc"):
        distribution = importlib.metadata.distribution(package)
        package_dir = destination / package
        package_dir.mkdir()
        copied = 0
        for item in distribution.files or ():
            if any(word in item.name.lower() for word in ("license", "copying", "notice")):
                source = Path(distribution.locate_file(item))
                if source.is_file():
                    shutil.copy2(source, package_dir / item.name)
                    copied += 1
        if not copied:
            raise RuntimeError(f"No installed license text found for {package}")
    sodium_license = next(
        (parent / name for parent in libsodium.parents
         for name in ("LICENSE", "LICENSE.txt", "COPYING")
         if (parent / name).is_file()), None,
    )
    if sodium_license is None:
        raise RuntimeError("Place the libsodium LICENSE beside the DLL or in a parent directory")
    shutil.copy2(sodium_license, destination / "libsodium-LICENSE.txt")

    # Ask the exact runtime being shipped for its own and bundled third-party
    # notices, rather than fetching a potentially different source version.
    script = work / "dump_runtime_licenses.gd"
    (work / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    script.write_text(
        'extends SceneTree\n'
        'func _initialize():\n'
        '\tvar output = OS.get_cmdline_user_args()[0]\n'
        '\tvar file = FileAccess.open(output, FileAccess.WRITE)\n'
        '\tif file == null:\n\t\tquit(1)\n\t\treturn\n'
        '\tfile.store_string(JSON.stringify({\n'
        '\t\t"engine_license": Engine.get_license_text(),\n'
        '\t\t"third_party_licenses": Engine.get_license_info(),\n'
        '\t\t"copyright": Engine.get_copyright_info()\n'
        '\t}, "\\t"))\n'
        '\tfile.close()\n\tquit()\n', encoding="utf-8",
    )
    output = destination / "Godot-NOTICES.json"
    run([godot, "--headless", "--path", work, "--script", script,
         "--", output], log=work / "licenses.log")
    notices = json.loads(output.read_text(encoding="utf-8"))
    (destination / "Godot-LICENSE.txt").write_text(
        notices["engine_license"], encoding="utf-8",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--godot", required=True, type=Path,
                        help="Installed Windows Godot runtime executable")
    parser.add_argument("--libsodium", required=True, type=Path,
                        help="Ristretto-enabled libsodium DLL; its LICENSE must be nearby")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist" / "windows-preview",
                        help="Parent for a new timestamped preview folder and ZIP")
    parser.add_argument("--self-test", action="store_true",
                        help="Require the frozen ten-hand Godot check before creating the ZIP")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("Build the Windows preview with Windows Python")
    args.godot = args.godot.expanduser().resolve(strict=True)
    args.libsodium = args.libsodium.expanduser().resolve(strict=True)
    if not args.godot.is_file() or not args.libsodium.is_file():
        parser.error("--godot and --libsodium must name files")
    if not (ROOT / "holdem" / "desktop_launcher.py").is_file():
        parser.error("holdem.desktop_launcher is not present in this checkout")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    build_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
    work = ROOT / "build" / "windows-preview" / build_id
    work.mkdir(parents=True, exist_ok=False)
    output = args.output_dir.expanduser().resolve() / f"Poker-preview-{build_id}"
    output.mkdir(parents=True, exist_ok=False)
    assets = work / "assets"
    assets.mkdir()

    # A staged project avoids modifying tracked import metadata or packing the
    # editor/test suite. The headless real-client driver remains in sidecar/.
    project = work / "godot-project"
    shutil.copytree(
        ROOT / "godot", project,
        ignore=shutil.ignore_patterns(".godot", "addons", "test", "*.tmp"),
    )
    project_file = project / "project.godot"
    source = project_file.read_text(encoding="utf-8")
    source = re.sub(r"(?ms)^\[editor_plugins\]\s*.*?(?=^\[|\Z)", "", source)
    project_file.write_text(source, encoding="utf-8")
    version = run([args.godot, "--version"]).strip()
    run([args.godot, "--headless", "--path", project, "--import"],
        log=work / "godot-import.log")
    run([args.godot, "--headless", "--path", project, "--export-pack",
         "Windows Preview Pack", assets / "poker.pck"],
        log=work / "godot-export.log")
    if not (assets / "poker.pck").is_file():
        raise RuntimeError("Godot did not produce poker.pck")
    shutil.copy2(args.godot, assets / "PokerRuntime.exe")
    native = work / "native"
    native.mkdir()
    staged_sodium = native / "libsodium.dll"
    shutil.copy2(args.libsodium, staged_sodium)

    environment = dict(os.environ)
    environment.update(
        POKER_PROJECT_ROOT=str(ROOT), POKER_BUILD_ASSETS=str(assets),
        POKER_LIBSODIUM=str(staged_sodium),
    )
    run([sys.executable, "-m", "PyInstaller", "--noconfirm",
         "--distpath", output, "--workpath", work / "pyinstaller",
         ROOT / "packaging" / "windows.spec"], env=environment,
        log=work / "pyinstaller.log")
    app = output / "Poker"
    collect_licenses(app / "licenses", args.godot, args.libsodium, work)
    (app / "README.txt").write_text(
        "Texas Hold'em - Windows preview\n\n"
        "Extract the entire ZIP, then double-click Poker.exe. Keep _internal beside it.\n"
        "Play against local bots, or host/join a play-money game with friends.\n"
        "No Python or Godot installation is needed.\n\n"
        "Internet: enter your name, choose Host Internet game and copy the invite.\n"
        "The host needs a compatible router with automatic port forwarding and public IPv4.\n"
        "LAN: choose Host LAN game for friends on the same local network.\n"
        "Friends paste either invite, choose Join game, then Ready.\n"
        "The host selects Start game once everyone is ready.\n"
        "Turns automatically check or fold after 30 seconds.\n"
        "After each hand, select Next hand or wait for its 30-second countdown.\n"
        "If a guest leaves, remaining players continue after their results agree.\n"
        "If the host leaves, the table closes. All players need this preview version.\n"
        "Leave table returns to the menu and releases the host's temporary port mapping.\n"
        "No reconnect in this preview. Some networks cannot host a direct Internet game.\n\n"
        "Diagnostic check: Poker.exe --self-test\n"
        "Source: https://github.com/nephrium83/texas-holdem\n"
        "License notices are in licenses/.\n", encoding="utf-8",
    )
    self_test = None
    if args.self_test:
        test_logs = work / "self-test"
        isolated_environment = dict(os.environ)
        windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
        isolated_environment["PATH"] = os.pathsep.join(
            str(path) for path in (windows / "System32", windows, windows / "System32" / "Wbem")
        )
        for variable in ("PYTHONPATH", "PYTHONHOME", "GODOT_BIN", "HOLDEM_LIBSODIUM"):
            isolated_environment.pop(variable, None)
        isolated_environment["HOLDEM_CONFIG_DIR"] = str(test_logs / "config")
        run([app / "Poker.exe", "--self-test", "--log-dir", test_logs],
            cwd=output, env=isolated_environment, log=work / "frozen-self-test.log")
        self_test = json.loads((test_logs / "self-test.json").read_text(encoding="utf-8"))
        if not self_test.get("ok") or (self_test.get("summary") or {}).get("hands_settled", 0) < 10:
            raise RuntimeError(f"Frozen ten-hand check failed; see {test_logs}")
    commit = run(["git", "rev-parse", "HEAD"]).strip()
    source_dirty = bool(run(["git", "status", "--porcelain"]).strip())
    source_paths = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
    ).split(b"\0")
    source_hashes = {
        os.fsdecode(path).replace("\\", "/"): sha256(ROOT / os.fsdecode(path))
        for path in sorted(set(source_paths)) if path and (ROOT / os.fsdecode(path)).is_file()
    }
    source_digest = hashlib.sha256(json.dumps(
        source_hashes, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    manifest = {
        "built_at_utc": stamp, "source_commit": commit,
        "source_dirty": source_dirty,
        "source_tree_sha256": source_digest,
        "source_files_sha256": source_hashes,
        "python_version": sys.version, "godot_version": version,
        "pyinstaller_version": importlib.metadata.version("pyinstaller"),
        "libsodium_sha256": sha256(args.libsodium),
        "godot_sha256": sha256(args.godot),
        "self_test": self_test,
        "files": {str(p.relative_to(app)).replace("\\", "/"): sha256(p)
                  for p in sorted(app.rglob("*")) if p.is_file()},
    }
    (app / "build-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8",
    )
    archive = output.with_suffix(".zip")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zipped:
        for path in sorted(app.rglob("*")):
            if path.is_file():
                zipped.write(path, path.relative_to(app))
    print(json.dumps({"application": str(app / "Poker.exe"), "zip": str(archive),
                      "sha256": sha256(archive), "build_logs": str(work)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
