"""Evidence checks for the separately frozen engineering probe."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ice_probe_build", ROOT / "tools" / "build_ice_probe.py")
sys.path.insert(0, str(ROOT / "tools"))
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


def _application(tmp_path):
    application = tmp_path / "DirectConnectProbe"
    native = application / "_internal" / "native.dll"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"test native library")
    (application / "DirectConnectProbe.exe").write_bytes(b"test executable")
    (application / "README.txt").write_text("Test probe", encoding="utf-8")
    files = {str(p.relative_to(application)).replace("\\", "/"): build.sha256(p)
             for p in application.rglob("*") if p.is_file()}
    manifest = {"files_sha256": files,
                "native_files_sha256": {name: value for name, value in files.items()
                                        if Path(name).suffix in (".exe", ".dll")}}
    (application / "build-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return application, native


@pytest.mark.parametrize("mutation", ["remove", "replace", "extra", "native-inventory"])
def test_manifest_refuses_missing_modified_or_uninventoried_files(tmp_path, mutation):
    application, native = _application(tmp_path)
    build.verify_manifest(application)
    if mutation == "remove":
        native.unlink()
    elif mutation == "replace":
        native.write_bytes(b"different DLL")
    elif mutation == "extra":
        (application / "_internal" / "unrecorded.dll").write_bytes(b"extra DLL")
    else:
        path = application / "build-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["native_files_sha256"] = {}
        path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="hashes|coverage"):
        build.verify_manifest(application)


def test_reviewed_native_notices_have_exact_hash_coverage():
    root = build.NOTICE_ROOT
    index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    actual = {str(p.relative_to(root)).replace("\\", "/"): build.sha256(p)
              for p in root.rglob("*") if p.is_file() and p.name != "index.json"}
    assert actual == index["files_sha256"]
    assert index["distribution_status"] == "minimal-lgpl-source-bundled"
    assert index["reviewed_distributions"]["av"] == "17.1.0+minimal.20261024"
    assert {component["name"] for component in index["native_components"]} >= {
        "ffmpeg", "libsrtp", "openssl", "libffi", "crc32c", "cryptography-rust",
    }
    assert not ({component["name"] for component in index["native_components"]} & {"x264", "x265"})


def test_stock_pyav_is_rejected_before_a_public_build():
    from types import SimpleNamespace
    with pytest.raises(RuntimeError, match="minimal PyAV"):
        build.verify_minimal_runtime(SimpleNamespace(version="17.1.0"))


def test_reviewed_minimal_runtime_has_exact_source_and_native_coverage():
    import importlib.metadata
    pytest.importorskip("av", reason="install the reviewed custom runtime for native provenance acceptance")
    distribution = importlib.metadata.distribution("av")
    if distribution.version != "17.1.0+minimal.20261024":
        pytest.skip("stock PyAV is intentionally not a distributable probe runtime")
    provenance, _, _ = build.verify_minimal_runtime(distribution)
    assert provenance["ffmpeg_source_modifications"] == []
    assert "--disable-everything" in provenance["ffmpeg_flags"]
    assert "--disable-autodetect" in provenance["ffmpeg_flags"]


def test_result_path_refuses_overwriting_before_build(tmp_path, monkeypatch):
    result = tmp_path / "already-reviewed.json"
    result.write_text("old artifact", encoding="utf-8")
    monkeypatch.setattr(build.sys, "platform", "win32")
    with pytest.raises(SystemExit) as error:
        build.main(["--result-json", str(result)])
    assert error.value.code == 2
    assert result.read_text(encoding="utf-8") == "old artifact"


@pytest.mark.timeout(90)
def test_frozen_probe_runs_after_relocation_without_host_runtime(tmp_path):
    configured = os.environ.get("HOLDEM_ICE_PROBE_EXE")
    if not configured:
        pytest.skip("set HOLDEM_ICE_PROBE_EXE to test the frozen connectivity probe")
    if sys.platform != "win32":
        pytest.skip("the frozen engineering probe runs on Windows")
    source = Path(configured).resolve(strict=True)
    copied = tmp_path / "Relocated direct connection probe"
    shutil.copytree(source.parent, copied)
    build.verify_manifest(copied)
    outside = tmp_path / "unrelated working folder"
    outside.mkdir()
    result = subprocess.run(
        [str(copied / source.name), "--self-test"], cwd=outside,
        env=build.cold_environment(), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "passed", report
    assert report["direct"] is True and report["protocol"] == "udp", report
    assert report["peer_closed"] is True and report["owned_tasks_pending"] == 0, report
    assert report["sizes"] == [4096, 32768, 524288], report
