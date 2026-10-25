"""Freeze the optional WebRTC engineering probe as a fresh Windows folder/ZIP.

Install this build environment using binary wheels only, for example::

    python -m pip install --only-binary=:all: .[internet] pyinstaller==6.20.0
    python tools/build_ice_probe.py --self-test --result-json build/probe-result.json

This tool never installs dependencies, changes the normal Poker preview, edits
router/firewall rules, or overwrites a previous application/ZIP. It intentionally
fails when an installed native dependency has no reviewed notice coverage.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
NOTICE_ROOT = ROOT / "packaging" / "ice_probe_licenses"


def verify_minimal_runtime(distribution):
    """Reject stock/altered native closures before creating a public package."""
    from build_ice_native import FLAGS, INPUTS, VERSION

    if distribution.version != VERSION:
        raise RuntimeError("The public probe requires the reviewed minimal PyAV build")
    files = {str(item).replace("\\", "/"): item for item in distribution.files or ()}
    candidates = [name for name in files if name.endswith(".dist-info/ice-minimal-provenance.json")]
    if len(candidates) != 1:
        raise RuntimeError("Minimal PyAV source/build provenance is missing")
    path = candidates[0]
    provenance = json.loads(Path(distribution.locate_file(files[path])).read_text(encoding="utf-8"))
    if (provenance.get("schema") != "holdem-minimal-pyav-v1" or
            provenance.get("version") != VERSION or
            provenance.get("distribution_status") != "minimal-lgpl-source-bundled" or
            provenance.get("inputs") != INPUTS or provenance.get("ffmpeg_flags") != FLAGS or
            provenance.get("ffmpeg_check") != {
                "license": "LGPL version 2.1 or later", "configuration": " ".join(FLAGS)} or
            provenance.get("ffmpeg_source_modifications") != [] or
            provenance.get("pyav_source_modifications") != [{"file": "av/about.py", "version": VERSION}]):
        raise RuntimeError("Minimal PyAV provenance disagrees with reviewed inputs/configuration")
    native = {name: sha256(distribution.locate_file(item)) for name, item in files.items()
              if Path(name).suffix.lower() in (".pyd", ".dll")}
    if native != provenance.get("native_files_sha256"):
        raise RuntimeError("Installed PyAV native files differ from the reviewed build")
    prefix = path.rsplit("/", 1)[0] + "/"
    source_files = provenance.get("source_bundle_files_sha256", {})
    expected_sources = {
        "minimal-sources/native-build-inputs.json", "minimal-sources/FFmpeg-configure-flags.json",
        "minimal-sources/RELINK.txt", "minimal-sources/build_ice_native.py",
        "minimal-sources/FFmpeg-COPYING.LGPLv2.1.txt", "minimal-sources/FFmpeg-LICENSE.md",
        "minimal-sources/" + INPUTS["ffmpeg"]["filename"],
        "minimal-sources/" + INPUTS["pyav"]["filename"],
    }
    if set(source_files) != expected_sources:
        raise RuntimeError("Minimal PyAV corresponding source/relink bundle is incomplete")
    for relative, expected in source_files.items():
        if prefix + relative not in files or sha256(distribution.locate_file(files[prefix + relative])) != expected:
            raise RuntimeError("Minimal PyAV source bundle digest mismatch")
    for name in ("ffmpeg", "pyav"):
        if source_files["minimal-sources/" + INPUTS[name]["filename"]] != INPUTS[name]["sha256"]:
            raise RuntimeError("Minimal PyAV original source digest mismatch")
    if source_files["minimal-sources/build_ice_native.py"] != sha256(ROOT / "tools" / "build_ice_native.py"):
        raise RuntimeError("Minimal PyAV rebuild instructions use a different build script")
    return provenance, prefix, files


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(command, *, cwd=ROOT, env=None, log=None, timeout=300):
    command = [str(part) for part in command]
    print("Running:", subprocess.list2cmdline(command), flush=True)
    completed = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False, timeout=timeout,
    )
    combined = completed.stdout + "\n--- stderr ---\n" + completed.stderr
    if log is not None:
        Path(log).write_text(combined, encoding="utf-8")
    if completed.returncode:
        print(combined[-12000:], flush=True)
        raise RuntimeError(
            f"Command failed ({completed.returncode}); log: {log or 'above'}")
    return completed.stdout


def source_snapshot():
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True).strip()
    status = subprocess.check_output(
        ["git", "status", "--porcelain=v1"], cwd=ROOT, text=True)
    paths = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
    ).split(b"\0")
    hashes = {
        os.fsdecode(path).replace("\\", "/"): sha256(ROOT / os.fsdecode(path))
        for path in sorted(set(paths)) if path and (ROOT / os.fsdecode(path)).is_file()
    }
    digest = hashlib.sha256(json.dumps(
        hashes, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {
        "source_commit": commit, "source_git_tree": tree,
        "source_dirty": bool(status.strip()), "source_status": status.splitlines(),
        "source_tree_sha256": digest, "source_files_sha256": hashes,
    }


def runtime_distributions():
    """Resolve the installed, marker-filtered runtime closure, not build tools."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    pending = ["aiortc"]
    selected = {}
    while pending:
        requested = pending.pop()
        key = canonicalize_name(requested)
        if key in selected:
            continue
        distribution = importlib.metadata.distribution(requested)
        selected[key] = distribution
        for requirement in distribution.requires or ():
            parsed = Requirement(requirement)
            if parsed.marker is not None and not parsed.marker.evaluate({"extra": ""}):
                continue
            dependency = importlib.metadata.distribution(parsed.name)
            if parsed.specifier and dependency.version not in parsed.specifier:
                raise RuntimeError(f"Unsatisfied installed dependency: {requirement}")
            pending.append(parsed.name)
    if selected["aiortc"].version != "1.15.0":
        raise RuntimeError("This reviewed probe requires aiortc==1.15.0")
    if selected["aioice"].version != "0.10.2":
        raise RuntimeError("This reviewed peer lifecycle requires aioice==0.10.2")
    return dict(sorted(selected.items()))


def collect_licenses(destination, distributions):
    """Keep installed notices verbatim, adding reviewed native-wheel gaps."""
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copy2(ROOT / "LICENSE", destination / "Poker-MIT-LICENSE.txt")
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError(f"Python license is missing: {python_license}")
    shutil.copy2(python_license, destination / "Python-LICENSE.txt")
    notice_index = json.loads((NOTICE_ROOT / "index.json").read_text(encoding="utf-8"))
    if notice_index.get("distribution_status") != "minimal-lgpl-source-bundled":
        raise RuntimeError("Native redistribution/source review is incomplete")
    provenance, av_prefix, av_files = verify_minimal_runtime(distributions["av"])
    for name, version in notice_index["reviewed_distributions"].items():
        if name not in distributions or distributions[name].version != version:
            raise RuntimeError(
                f"Native notice coverage requires {name}=={version}; review before upgrading")
    for relative, expected in notice_index["files_sha256"].items():
        candidate = NOTICE_ROOT / relative
        if sha256(candidate) != expected:
            raise RuntimeError(f"Reviewed notice changed: {relative}")
    shutil.copytree(NOTICE_ROOT, destination / "native-provenance")
    for relative in provenance["source_bundle_files_sha256"]:
        source = Path(distributions["av"].locate_file(av_files[av_prefix + relative]))
        output = destination / "native-provenance" / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, output)
    (destination / "native-provenance" / "minimal-runtime.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    coverage = {}
    for name, distribution in distributions.items():
        target = destination / "python-packages" / name
        target.mkdir(parents=True)
        copied = []
        for item in distribution.files or ():
            if not any(term in item.name.lower() for term in ("license", "notice", "copying")):
                continue
            source = Path(distribution.locate_file(item))
            if not source.is_file() or source.suffix.lower() in (".pyc", ".pyo"):
                continue
            # Include the dist-info-relative path to avoid flattening repeated names.
            relative = Path(*[part for part in item.parts if part not in ("..", ".")])
            output = target / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, output)
            copied.append(str(output.relative_to(destination)).replace("\\", "/"))
        if not copied:
            fallback = notice_index.get("python_license_fallbacks", {}).get(name)
            if not fallback or distribution.version != fallback["version"]:
                raise RuntimeError(f"No reviewed license text for {name}=={distribution.version}")
            copied.append("native-provenance/" + fallback["path"])
        metadata_text = distribution.read_text("METADATA")
        if metadata_text is None:
            raise RuntimeError(f"No METADATA for installed {name}")
        (target / "METADATA.txt").write_text(metadata_text, encoding="utf-8")
        coverage[name] = {"version": distribution.version, "notices": copied}
    # The bootloader has a separate redistribution exception; ship its text too.
    bootloader = importlib.metadata.distribution("pyinstaller")
    bootloader_target = destination / "PyInstaller"
    bootloader_target.mkdir()
    copied_bootloader = 0
    for item in bootloader.files or ():
        if "copying" not in item.name.lower() and "license" not in item.name.lower():
            continue
        source = Path(bootloader.locate_file(item))
        if source.is_file():
            shutil.copy2(source, bootloader_target / item.name)
            copied_bootloader += 1
    if not copied_bootloader:
        raise RuntimeError("PyInstaller bootloader license/exception is missing")
    return {"python_packages": coverage, "native": notice_index["native_components"],
            "minimal_runtime": provenance,
            "distribution_status": notice_index["distribution_status"]}


def cold_environment():
    environment = dict(os.environ)
    windows = Path(environment.get("SystemRoot", "C:/Windows"))
    environment["PATH"] = os.pathsep.join(
        str(path) for path in (windows / "System32", windows, windows / "System32" / "Wbem"))
    for variable in ("PYTHONPATH", "PYTHONHOME", "GODOT_BIN", "HOLDEM_LIBSODIUM"):
        environment.pop(variable, None)
    return environment


def verify_manifest(application):
    """Verify exact file coverage, so a missing DLL cannot be passed as portable."""
    manifest_path = application / "build-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual = {
        str(path.relative_to(application)).replace("\\", "/"): sha256(path)
        for path in sorted(application.rglob("*"))
        if path.is_file() and path != manifest_path
    }
    if actual != manifest["files_sha256"]:
        raise RuntimeError("Frozen probe file hashes/coverage disagree with the manifest")
    native = {name: digest for name, digest in actual.items()
              if Path(name).suffix.lower() in (".exe", ".dll", ".pyd")}
    if native != manifest["native_files_sha256"]:
        raise RuntimeError("Native dependency hashes disagree with the manifest")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist" / "ice-probe")
    parser.add_argument("--result-json", type=Path,
                        help="Write output paths/hashes here; an existing file is not overwritten")
    parser.add_argument("--self-test", action="store_true",
                        help="Require the frozen local-pair check with STUN disabled")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("Build this Windows probe using Windows Python")
    if not (ROOT / "holdem" / "ice_probe.py").is_file():
        parser.error("holdem.ice_probe is not present in this checkout")
    if args.result_json is not None:
        args.result_json = args.result_json.expanduser().resolve()
        if args.result_json.exists():
            parser.error("--result-json already exists; use a fresh path")
    source = source_snapshot()
    distributions = runtime_distributions()
    verify_minimal_runtime(distributions["av"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    build_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
    work = ROOT / "build" / "ice-probe" / build_id
    work.mkdir(parents=True, exist_ok=False)
    output = args.output_dir.expanduser().resolve() / f"DirectConnectProbe-{build_id}"
    output.mkdir(parents=True, exist_ok=False)
    native_probe = run(
        [sys.executable, ROOT / "tools" / "ice_probe_native_check.py"],
        log=work / "native-import-check.log", timeout=30,
    )
    native_report = json.loads(native_probe)
    if not native_report.get("ok"):
        raise RuntimeError("Native WebRTC dependency check failed")
    environment = dict(os.environ)
    environment["POKER_PROJECT_ROOT"] = str(ROOT)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", output, "--workpath", work / "pyinstaller",
         ROOT / "packaging" / "ice_probe.spec"],
        env=environment, log=work / "pyinstaller.log", timeout=600)
    application = output / "DirectConnectProbe"
    executable = application / "DirectConnectProbe.exe"
    if not executable.is_file():
        raise RuntimeError("PyInstaller did not create DirectConnectProbe.exe")
    licenses = collect_licenses(application / "licenses", distributions)
    (application / "README.txt").write_text(
        "DirectConnectProbe - engineering connectivity proof\n\n"
        "Extract the complete ZIP and keep _internal beside DirectConnectProbe.exe.\n"
        "No Python, Godot, VPN, or router rule is required.\n"
        "This is a separate diagnostic; it does not install or update Poker Preview.\n"
        "Run DirectConnectProbe.exe --help for the reviewed test modes.\n"
        "Run DirectConnectProbe.exe --self-test for a local pair without STUN.\n"
        "Local-pair success does not prove Internet hole punching.\n"
        "Public Internet proof requires this same ZIP/SHA at both endpoints.\n\n"
        "Dependency notices and native source provenance are in licenses/.\n"
        "This build uses minimal shared LGPL FFmpeg libraries with media codecs disabled.\n"
        "Corresponding source and replacement instructions: licenses/native-provenance/minimal-sources/.\n",
        encoding="utf-8",
    )
    self_test = None
    if args.self_test:
        outside = work / "unrelated-working-folder"
        outside.mkdir()
        text = run([executable, "--self-test"], cwd=outside,
                   env=cold_environment(), log=work / "frozen-self-test.log", timeout=60)
        self_test = json.loads(text)
        if (self_test.get("status") != "passed" or self_test.get("direct") is not True or
                self_test.get("protocol") != "udp" or self_test.get("peer_closed") is not True or
                self_test.get("owned_tasks_pending") != 0 or
                self_test.get("sizes") != [4096, 32768, 524288]):
            raise RuntimeError(f"Frozen local pair failed; see {work / 'frozen-self-test.log'}")
    if source_snapshot() != source:
        raise RuntimeError("Source changed while freezing the probe; rebuild the final revision")
    files = {
        str(path.relative_to(application)).replace("\\", "/"): sha256(path)
        for path in sorted(application.rglob("*")) if path.is_file()
    }
    manifest = {
        "format": "holdem-direct-probe-build-v1", "built_at_utc": stamp, **source,
        "python_version": sys.version,
        "python_executable_sha256": sha256(sys.executable),
        "pyinstaller_version": importlib.metadata.version("pyinstaller"),
        "package_versions": {name: dist.version for name, dist in distributions.items()},
        "native_import_check": native_report, "license_coverage": licenses,
        "self_test": self_test, "files_sha256": files,
        "native_files_sha256": {name: digest for name, digest in files.items()
                                if Path(name).suffix.lower() in (".exe", ".dll", ".pyd")},
    }
    manifest_path = application / "build-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    verify_manifest(application)
    archive = output.with_suffix(".zip")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zipped:
        for path in sorted(application.rglob("*")):
            if path.is_file():
                zipped.write(path, Path("DirectConnectProbe") / path.relative_to(application))
    result = {
        "application": str(executable), "zip": str(archive), "zip_sha256": sha256(archive),
        "manifest": str(manifest_path), "manifest_sha256": sha256(manifest_path),
        "build_logs": str(work), "self_test": self_test,
    }
    if args.result_json is not None:
        args.result_json.parent.mkdir(parents=True, exist_ok=True)
        with args.result_json.open("x", encoding="utf-8") as target:
            target.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
