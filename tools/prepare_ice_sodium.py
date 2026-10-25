"""Prepare the pinned official Windows x64 libsodium runtime for frozen poker.

Only the selected release DLL and its exact ISC notice are redistributed.
The result verifier performs no downloads and never discovers a developer DLL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.0.22"
ARCHIVE_NAME = "libsodium-1.0.22-msvc.zip"
ARCHIVE_URL = "https://github.com/jedisct1/libsodium/releases/download/1.0.22-RELEASE/" + ARCHIVE_NAME
ARCHIVE_SHA256 = "3e03a726fac4bc09cb61d8f29d658ef7a5eca0811de59082130414f7ca2e4279"
ARCHIVE_SIZE = 17690194
DLL_MEMBER = "libsodium/x64/Release/v143/dynamic/libsodium.dll"
DLL_SHA256 = "f656aeb789bfc3a2ac587fae1d7cfe278bbe8ce73f28da73b4d08282ea8f9fe3"
DLL_SIZE = 462848
SOURCE_COMMIT = "77e1ce5d6dee871c49ef211222ba18ef0c486bda"
LICENSE_URL = f"https://raw.githubusercontent.com/jedisct1/libsodium/{SOURCE_COMMIT}/LICENSE"
LICENSE_NAME = "libsodium-ISC.txt"
LICENSE_SHA256 = "508a76d186356c0dd807a670ef510964f8724557024796a2c426c6c0e19ab683"
LICENSE_SIZE = 823
SOURCE = {
    "url": ARCHIVE_URL, "sha256": ARCHIVE_SHA256, "member": DLL_MEMBER,
    "commit": SOURCE_COMMIT, "license_url": LICENSE_URL,
}
ATTRIBUTION = f"""libsodium {VERSION} — official Windows x64 release runtime

Project: https://github.com/jedisct1/libsodium
Release: https://github.com/jedisct1/libsodium/releases/tag/1.0.22-RELEASE
Official MSVC archive: {ARCHIVE_URL}
Archive SHA256: {ARCHIVE_SHA256}
Unmodified selected member: {DLL_MEMBER}
DLL SHA256: {DLL_SHA256}
Release source commit: {SOURCE_COMMIT}
Corresponding source: https://github.com/jedisct1/libsodium/tree/{SOURCE_COMMIT}
Exact ISC license notice: {LICENSE_URL}
License SHA256: {LICENSE_SHA256}

The release MSVC archive does not include a license file. The accompanying
libsodium-ISC.txt is copied verbatim from this release's source commit.
""".encode("utf-8")


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _read_json(path):
    path = Path(path)
    if path.stat().st_size > 65536:
        raise RuntimeError("Oversized libsodium result or manifest")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise RuntimeError("Duplicate libsodium metadata key")
            result[key] = value
        return result

    return json.loads(path.read_bytes(), object_pairs_hook=pairs)


def _fetch(cache, name, url, expected_sha256, expected_size):
    path = cache / name
    if not path.exists():
        temporary = cache / (name + ".download-" + uuid.uuid4().hex)
        try:
            curl = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "curl.exe"
            result = subprocess.run(
                [str(curl), "--fail", "--location", "--silent", "--show-error",
                 "--max-time", "180", "--proto", "=https", "--proto-redir", "=https",
                 "--output", str(temporary), url],
                capture_output=True, text=True, timeout=190,
            )
            if result.returncode:
                raise RuntimeError(f"Could not download pinned libsodium input: {name}")
            _check_file(temporary, expected_sha256, expected_size)
            # An existing cache entry is never silently replaced.
            with path.open("xb") as target:
                target.write(temporary.read_bytes())
        finally:
            temporary.unlink(missing_ok=True)
    _check_file(path, expected_sha256, expected_size)
    return path


def _check_file(path, expected_sha256, expected_size):
    path = Path(path)
    if not path.is_file() or path.stat().st_size != expected_size or digest(path) != expected_sha256:
        raise RuntimeError(f"Pinned libsodium file mismatch: {path.name}")


def _manifest():
    return {
        "v": 1, "version": VERSION, "source": SOURCE,
        "files": {
            "native/libsodium.dll": {"sha256": DLL_SHA256, "size": DLL_SIZE},
            "licenses/libsodium-ISC.txt": {"sha256": LICENSE_SHA256, "size": LICENSE_SIZE},
            "licenses/libsodium-SOURCE.txt": {
                "sha256": hashlib.sha256(ATTRIBUTION).hexdigest(), "size": len(ATTRIBUTION),
            },
        },
        "abi": {"version": VERSION, "ristretto255": True},
    }


def _verify_abi(dll):
    """Check the exact file in a new process with discovery forbidden."""
    script = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from holdem import native_crypto as N
from holdem.p2p import ristretto as R
def forbidden():
    raise AssertionError('explicit native startup attempted discovery')
N._candidate_paths = forbidden
path = Path(sys.argv[2]).resolve(strict=True)
backend = N.initialize(library_path=path)
assert Path(backend._library._name).resolve(strict=True) == path
assert backend.version() == sys.argv[3]
one = R.Scalar(bytes([1]) + bytes(31))
assert bytes(R.mul_base(one)) == bytes(R.G)
assert R.G.is_valid()
assert bytes(R.scalar_invert(one)) == bytes(one)
assert R.hash_to_group(bytes(range(64))).is_valid()
print(json.dumps({'version': backend.version(), 'loaded_path': str(path)}))
'''
    environment = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "HOLDEM_LIBSODIUM"):
        environment.pop(key, None)
    environment["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(ROOT), str(dll), VERSION],
        cwd=dll.parent, env=environment, capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Selected libsodium runtime failed isolated ABI/operation checks: " + result.stderr.strip())
    receipt = json.loads(result.stdout)
    if receipt != {"version": VERSION, "loaded_path": str(dll.resolve(strict=True))}:
        raise RuntimeError("Unexpected libsodium ABI receipt")
    return receipt


def prepare(*, cache_dir, output_dir):
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        raise RuntimeError("The pinned runtime requires Windows x64 Python")
    cache = Path(cache_dir).resolve()
    output = Path(output_dir).resolve()
    if output.exists():
        raise RuntimeError("libsodium output directory already exists; select a fresh directory")
    cache.mkdir(parents=True, exist_ok=True)
    archive = _fetch(cache, ARCHIVE_NAME, ARCHIVE_URL, ARCHIVE_SHA256, ARCHIVE_SIZE)
    notice = _fetch(cache, LICENSE_NAME, LICENSE_URL, LICENSE_SHA256, LICENSE_SIZE)
    with zipfile.ZipFile(archive) as source:
        matches = [info for info in source.infolist() if info.filename == DLL_MEMBER]
        if len(matches) != 1 or matches[0].file_size != DLL_SIZE:
            raise RuntimeError("Unexpected official libsodium DLL archive member")
        binary = source.read(matches[0])
    if hashlib.sha256(binary).hexdigest() != DLL_SHA256:
        raise RuntimeError("Official libsodium DLL digest mismatch")
    output.mkdir(parents=True)
    (output / "native").mkdir()
    (output / "licenses").mkdir()
    dll = output / "native" / "libsodium.dll"
    license_path = output / "licenses" / LICENSE_NAME
    attribution = output / "licenses" / "libsodium-SOURCE.txt"
    manifest = output / "sodium-manifest.json"
    dll.write_bytes(binary)
    license_path.write_bytes(notice.read_bytes())
    attribution.write_bytes(ATTRIBUTION)
    abi = _verify_abi(dll)
    manifest.write_bytes(_json_bytes(_manifest()))
    return {
        "v": 1, "version": VERSION, "dll": str(dll), "dll_sha256": DLL_SHA256,
        "license": str(license_path), "license_sha256": LICENSE_SHA256,
        "attribution": str(attribution), "attribution_sha256": digest(attribution),
        "manifest": str(manifest), "manifest_sha256": digest(manifest), "abi": abi,
    }


def verify_result(path):
    """Verify prepared files and portable provenance without network or loading."""
    result = _read_json(path)
    required = {"v", "version", "dll", "dll_sha256", "license", "license_sha256",
                "attribution", "attribution_sha256", "manifest", "manifest_sha256", "abi"}
    if not isinstance(result, dict) or set(result) != required or result["v"] != 1 or result["version"] != VERSION:
        raise RuntimeError("Unexpected libsodium result schema")
    manifest = Path(result["manifest"])
    if not manifest.is_absolute() or manifest.name != "sodium-manifest.json":
        raise RuntimeError("Unexpected libsodium manifest path")
    directory = manifest.resolve(strict=True).parent
    expected = _manifest()
    _check_file(manifest, hashlib.sha256(_json_bytes(expected)).hexdigest(), len(_json_bytes(expected)))
    if result["manifest_sha256"] != digest(manifest) or _read_json(manifest) != expected:
        raise RuntimeError("libsodium provenance does not match the pinned runtime")
    names = {"dll": "native/libsodium.dll", "license": "licenses/libsodium-ISC.txt",
             "attribution": "licenses/libsodium-SOURCE.txt"}
    for key, relative in names.items():
        selected = Path(result[key])
        actual = (directory / relative).resolve(strict=True)
        if not selected.is_absolute() or selected.resolve(strict=True) != actual or not actual.is_relative_to(directory):
            raise RuntimeError("libsodium result files must belong to the prepared directory")
        file_info = expected["files"][relative]
        if result[key + "_sha256"] != file_info["sha256"]:
            raise RuntimeError("libsodium result digest does not match the pinned runtime")
        _check_file(actual, file_info["sha256"], file_info["size"])
        result[key] = str(actual)
    if result["abi"] != {"version": VERSION, "loaded_path": result["dll"]}:
        raise RuntimeError("libsodium isolated ABI receipt does not match the selected DLL")
    result["manifest"] = str(manifest.resolve(strict=True))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "build" / "ice-sodium-inputs")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.result.exists():
        parser.error("result already exists; select a fresh result path")
    result = prepare(cache_dir=args.cache_dir, output_dir=args.output_dir)
    args.result.parent.mkdir(parents=True, exist_ok=True)
    with args.result.open("xb") as target:
        target.write(_json_bytes(result))
    verify_result(args.result)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
