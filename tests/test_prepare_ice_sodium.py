"""Pinned runtime preparation fails closed on altered cache/provenance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import zipfile

import pytest

from tools import prepare_ice_sodium as S


def test_cached_input_is_checked_before_any_download(tmp_path, monkeypatch):
    payload = b"pinned input"
    path = tmp_path / "input.zip"
    path.write_bytes(payload)
    monkeypatch.setattr(S.subprocess, "run", lambda *a, **k: pytest.fail("downloaded cached input"))
    assert S._fetch(tmp_path, path.name, "https://invalid.example", hashlib.sha256(payload).hexdigest(), len(payload)) == path
    path.write_bytes(b"altered")
    with pytest.raises(RuntimeError, match="Pinned libsodium file mismatch"):
        S._fetch(tmp_path, path.name, "https://invalid.example", hashlib.sha256(payload).hexdigest(), len(payload))


def test_result_rejects_duplicate_keys_and_oversized_metadata(tmp_path):
    path = tmp_path / "result.json"
    path.write_text('{"v":1,"v":1}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="Duplicate"):
        S.verify_result(path)
    path.write_bytes(b" " * 65537)
    with pytest.raises(RuntimeError, match="Oversized"):
        S.verify_result(path)


def test_portable_manifest_contains_no_build_machine_paths():
    manifest = S._manifest()
    assert manifest["source"]["commit"] == "77e1ce5d6dee871c49ef211222ba18ef0c486bda"
    assert manifest["files"]["native/libsodium.dll"]["sha256"] == S.DLL_SHA256
    assert all(not Path(path).is_absolute() for path in manifest["files"])
    assert str(S.ROOT) not in json.dumps(manifest)


@pytest.fixture
def pinned_result(tmp_path):
    # Local integration uses the official cached asset; CI never downloads native
    # libraries implicitly. The actual helper command performs a cold ABI check.
    archive = S.ROOT / "build" / "ice-sodium-inputs" / S.ARCHIVE_NAME
    notice = S.ROOT / "build" / "ice-sodium-inputs" / S.LICENSE_NAME
    if not archive.is_file() or not notice.is_file():
        pytest.skip("official native asset is not cached")
    S._check_file(archive, S.ARCHIVE_SHA256, S.ARCHIVE_SIZE)
    S._check_file(notice, S.LICENSE_SHA256, S.LICENSE_SIZE)
    output = tmp_path / "runtime"
    (output / "native").mkdir(parents=True)
    (output / "licenses").mkdir()
    dll = output / "native" / "libsodium.dll"
    license_path = output / "licenses" / S.LICENSE_NAME
    attribution = output / "licenses" / "libsodium-SOURCE.txt"
    manifest = output / "sodium-manifest.json"
    with zipfile.ZipFile(archive) as source:
        dll.write_bytes(source.read(S.DLL_MEMBER))
    shutil.copyfile(notice, license_path)
    attribution.write_bytes(S.ATTRIBUTION)
    manifest.write_bytes(S._json_bytes(S._manifest()))
    result = {
        "v": 1, "version": S.VERSION, "dll": str(dll), "dll_sha256": S.DLL_SHA256,
        "license": str(license_path), "license_sha256": S.LICENSE_SHA256,
        "attribution": str(attribution), "attribution_sha256": S.digest(attribution),
        "manifest": str(manifest), "manifest_sha256": S.digest(manifest),
        "abi": {"version": S.VERSION, "loaded_path": str(dll)},
    }
    path = tmp_path / "result.json"
    path.write_bytes(S._json_bytes(result))
    return path, result


def test_prepared_result_verifies_offline(pinned_result, monkeypatch):
    path, result = pinned_result
    monkeypatch.setattr(S.subprocess, "run", lambda *a, **k: pytest.fail("verification ran a subprocess"))
    assert S.verify_result(path) == result


@pytest.mark.parametrize("field", ["dll", "license", "attribution", "manifest"])
def test_tampered_native_payload_or_notice_is_rejected(pinned_result, field):
    path, result = pinned_result
    target = Path(result[field])
    target.write_bytes(target.read_bytes() + b"altered")
    with pytest.raises(RuntimeError, match="mismatch"):
        S.verify_result(path)


def test_result_cannot_point_at_an_unrelated_library(pinned_result, tmp_path):
    path, result = pinned_result
    unrelated = tmp_path / "unrelated.dll"
    shutil.copyfile(result["dll"], unrelated)
    result["dll"] = str(unrelated)
    result["abi"]["loaded_path"] = str(unrelated)
    path.write_bytes(S._json_bytes(result))
    with pytest.raises(RuntimeError, match="prepared directory"):
        S.verify_result(path)


def test_result_cannot_change_source_pins(pinned_result):
    path, result = pinned_result
    manifest = Path(result["manifest"])
    value = json.loads(manifest.read_bytes())
    value["source"]["sha256"] = "0" * 64
    manifest.write_bytes(S._json_bytes(value))
    result["manifest_sha256"] = S.digest(manifest)
    path.write_bytes(S._json_bytes(result))
    with pytest.raises(RuntimeError, match="mismatch"):
        S.verify_result(path)
