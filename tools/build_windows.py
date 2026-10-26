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
import sys
import uuid
import zipfile

import build_ice_probe as ice_build
from prepare_ice_sodium import verify_result as verify_sodium_result


ROOT = Path(__file__).resolve().parents[1]


def run(command, *, cwd=ROOT, env=None, log=None, timeout=300):
    return ice_build.run(command, cwd=cwd, env=env, log=log, timeout=timeout)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_distributions():
    """Use the reviewed ICE closure and the explicitly pinned setup client."""
    distributions = ice_build.runtime_distributions()
    websocket = importlib.metadata.distribution("websockets")
    if websocket.version != "16.1.1":
        raise RuntimeError("The desktop introduction client requires websockets==16.1.1")
    distributions["websockets"] = websocket
    return dict(sorted(distributions.items()))


def collect_licenses(destination, godot, sodium, work, distributions):
    coverage = ice_build.collect_licenses(destination, distributions)
    sodium_coverage = ice_build.collect_sodium_licenses(destination.parent, sodium)
    coverage["native"] = [*coverage["native"], {
        "name": "libsodium", "version": sodium["version"], "license": "ISC",
        "source": "libsodium/provenance.json", "notices": ["libsodium/LICENSE.txt", "libsodium/SOURCE.txt"],
    }]

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
    return coverage, sodium_coverage


def validate_internet_self_test(report):
    if not isinstance(report, dict) or report.get("ok") is not True:
        raise RuntimeError("Frozen desktop Internet runtime check failed")
    ice_build.validate_poker_self_test(report.get("summary"))
    return report


def verify_manifest(application):
    """Require complete file coverage plus the actual engine/native inputs."""
    path = application / "build-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    actual = {str(p.relative_to(application)).replace("\\", "/"): sha256(p)
              for p in sorted(application.rglob("*")) if p.is_file() and p != path}
    if "Poker.exe" not in actual:
        raise RuntimeError("Desktop package is missing its launcher")
    if actual != manifest["files"]:
        raise RuntimeError("Desktop package file hashes/coverage disagree with the manifest")
    native = {name: digest for name, digest in actual.items()
              if Path(name).suffix.lower() in (".exe", ".dll", ".pyd")}
    if native != manifest["native_files_sha256"]:
        raise RuntimeError("Desktop native hashes/coverage disagree with the manifest")
    if (actual.get("_internal/assets/PokerRuntime.exe") != manifest["godot_sha256"] or
            actual.get("_internal/assets/poker.pck") != manifest["pck_sha256"]):
        raise RuntimeError("Packaged Godot runtime/resources differ from the recorded inputs")
    sodium = manifest["libsodium"]
    if sodium["dll_sha256"] != manifest["libsodium_sha256"]:
        raise RuntimeError("Packaged libsodium input provenance differs")
    ice_build.verify_sodium_bundle(application, sodium)
    minimal = manifest["license_coverage"]["minimal_runtime"]["native_files_sha256"]
    if not minimal or not set(minimal.values()).issubset(set(native.values())):
        raise RuntimeError("Reviewed minimal PyAV native files are missing or replaced")
    if manifest["package_versions"].get("websockets") != "16.1.1":
        raise RuntimeError("Packaged introduction client does not match the reviewed version")
    if manifest.get("internet_self_test") is not None:
        validate_internet_self_test(manifest["internet_self_test"])
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--godot", required=True, type=Path,
                        help="Installed Windows Godot runtime executable")
    parser.add_argument("--sodium-result", required=True, type=Path,
                        help="Verified result from tools/prepare_ice_sodium.py")
    parser.add_argument("--libsodium", type=Path,
                        help="Optional explicit DLL; must match the verified sodium result")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist" / "windows-preview",
                        help="Parent for a new timestamped preview folder and ZIP")
    parser.add_argument("--self-test", action="store_true",
                        help="Require the frozen ten-hand Godot check before creating the ZIP")
    parser.add_argument("--internet-self-test", action="store_true",
                        help="Require the frozen ten-hand direct poker runtime check")
    parser.add_argument("--result-json", type=Path,
                        help="Write artifact paths/hashes to a fresh result file")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("Build the Windows preview with Windows Python")
    if args.result_json is not None:
        args.result_json = args.result_json.expanduser().resolve()
        if args.result_json.exists():
            parser.error("--result-json already exists; use a fresh path")
    args.godot = args.godot.expanduser().resolve(strict=True)
    sodium = verify_sodium_result(args.sodium_result.expanduser().resolve(strict=True))
    reviewed_sodium = Path(sodium["dll"])
    if args.libsodium is not None:
        args.libsodium = args.libsodium.expanduser().resolve(strict=True)
        if not args.libsodium.is_file() or sha256(args.libsodium) != sodium["dll_sha256"]:
            parser.error("--libsodium must match the verified official native input")
    args.libsodium = reviewed_sodium
    if not args.godot.is_file():
        parser.error("--godot must name a file")
    if not (ROOT / "holdem" / "desktop_launcher.py").is_file():
        parser.error("holdem.desktop_launcher is not present in this checkout")
    source = ice_build.source_snapshot()
    distributions = runtime_distributions()
    minimal_provenance, _, _ = ice_build.verify_minimal_runtime(distributions["av"])

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
    project_source = project_file.read_text(encoding="utf-8")
    project_source = re.sub(r"(?ms)^\[editor_plugins\]\s*.*?(?=^\[|\Z)", "", project_source)
    project_file.write_text(project_source, encoding="utf-8")
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
    native_report = json.loads(run(
        [sys.executable, ROOT / "tools" / "ice_probe_native_check.py"],
        log=work / "native-import-check.log", timeout=30))
    if (native_report.get("ok") is not True or
            native_report.get("av_version") != minimal_provenance["version"] or
            native_report.get("ffmpeg_license") != "LGPL version 2.1 or later" or
            native_report.get("ffmpeg_configuration", "").split() != minimal_provenance["ffmpeg_flags"]):
        raise RuntimeError("Loaded WebRTC native closure differs from the reviewed minimal source build")

    environment = dict(os.environ)
    environment.update(
        POKER_PROJECT_ROOT=str(ROOT), POKER_BUILD_ASSETS=str(assets),
        POKER_LIBSODIUM=str(staged_sodium),
    )
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", output, "--workpath", work / "pyinstaller",
         ROOT / "packaging" / "windows.spec"], env=environment,
        log=work / "pyinstaller.log", timeout=600)
    app = output / "Poker"
    licenses, sodium_coverage = collect_licenses(app / "licenses", args.godot, sodium, work, distributions)
    ice_build.verify_sodium_bundle(app, sodium_coverage)
    (app / "README.txt").write_text(
        "Texas Hold'em - Windows preview\n\n"
        "Extract the entire ZIP, then double-click Poker.exe. Keep _internal beside it.\n"
        "Play against local bots, or host/join play-money games over LAN or the Internet.\n"
        "No Python or Godot installation is needed.\n\n"
        "LAN: enter your name, choose Host LAN game and copy the invite.\n"
        "Friends paste the invite, choose Join LAN game, then Ready.\n"
        "Internet: choose Host Internet game and send its private invite to friends.\n"
        "Friends paste the invite and choose Join Internet game, then Ready.\n"
        "The configured introduction service exchanges setup; poker traffic goes directly between players.\n"
        "The host selects Start game once everyone is ready.\n"
        "Turns automatically check or fold after 30 seconds.\n"
        "After each hand, select Next hand or wait for its 30-second countdown.\n"
        "If a guest leaves, remaining players continue after their results agree.\n"
        "If the host leaves, the table closes. All players need this preview version.\n"
        "Leave table returns to the menu and also cancels a pending connection. No reconnect is offered.\n\n"
        "Diagnostic check: Poker.exe --self-test\n"
        "Source: https://github.com/nephrium83/texas-holdem\n"
        "License notices are in licenses/.\n", encoding="utf-8",
    )
    self_test = None
    internet_self_test = None
    isolated_environment = ice_build.cold_environment()
    isolated_environment.pop("HOLDEM_INTRODUCTION_URL", None)
    if args.self_test:
        test_logs = work / "self-test"
        isolated_environment["HOLDEM_CONFIG_DIR"] = str(test_logs / "config")
        run([app / "Poker.exe", "--self-test", "--log-dir", test_logs],
            cwd=output, env=isolated_environment, log=work / "frozen-self-test.log")
        self_test = json.loads((test_logs / "self-test.json").read_text(encoding="utf-8"))
        if not self_test.get("ok") or (self_test.get("summary") or {}).get("hands_settled", 0) < 10:
            raise RuntimeError(f"Frozen ten-hand check failed; see {test_logs}")
    if args.internet_self_test:
        test_logs = work / "internet-self-test"
        isolated_environment["HOLDEM_CONFIG_DIR"] = str(test_logs / "config")
        run([app / "Poker.exe", "--internet-runtime-self-test", "--log-dir", test_logs],
            cwd=output, env=isolated_environment, log=work / "frozen-internet-self-test.log", timeout=180)
        internet_self_test = validate_internet_self_test(json.loads(
            (test_logs / "internet-runtime-self-test.json").read_text(encoding="utf-8")))
    if ice_build.source_snapshot() != source:
        raise RuntimeError("Source changed while building the desktop; rebuild the final revision")
    files = {str(p.relative_to(app)).replace("\\", "/"): sha256(p)
             for p in sorted(app.rglob("*")) if p.is_file()}
    manifest = {
        "built_at_utc": stamp, **source,
        "python_version": sys.version, "godot_version": version,
        "pyinstaller_version": importlib.metadata.version("pyinstaller"),
        "libsodium_sha256": sha256(args.libsodium),
        "godot_sha256": sha256(args.godot),
        "pck_sha256": sha256(assets / "poker.pck"),
        "self_test": self_test, "internet_self_test": internet_self_test,
        "capabilities": ["local-bots", "authenticated-lan", "direct-internet"],
        "package_versions": {name: dist.version for name, dist in distributions.items()},
        "native_import_check": native_report, "license_coverage": licenses,
        "libsodium": sodium_coverage, "files": files,
        "native_files_sha256": {name: digest for name, digest in files.items()
                                if Path(name).suffix.lower() in (".exe", ".dll", ".pyd")},
    }
    (app / "build-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8",
    )
    verify_manifest(app)
    archive = output.with_suffix(".zip")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zipped:
        for path in sorted(app.rglob("*")):
            if path.is_file():
                zipped.write(path, path.relative_to(app))
    manifest_path = app / "build-manifest.json"
    result = {"application": str(app / "Poker.exe"), "zip": str(archive),
              "sha256": sha256(archive), "build_logs": str(work),
              "manifest": str(manifest_path), "manifest_sha256": sha256(manifest_path),
              "self_test": self_test, "internet_self_test": internet_self_test}
    if args.result_json is not None:
        args.result_json.parent.mkdir(parents=True, exist_ok=True)
        with args.result_json.open("x", encoding="utf-8") as target:
            target.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
