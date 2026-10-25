"""Explicitly build a minimal LGPL FFmpeg/PyAV wheel for the data-only probe.

This is an intentional source build, not a fallback for missing Windows wheels.
It uses the installed Windows MSVC/Git tools and an isolated Python environment.
Only the optional probe environment changes when --install is supplied.
"""
from __future__ import annotations

import argparse
import base64
import csv
import ctypes
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
VERSION = "17.1.0+minimal.20261024"
INPUTS = {
    "ffmpeg": {
        "filename": "ffmpeg-n8.1.3-source.tar.gz",
        "url": "https://github.com/FFmpeg/FFmpeg/archive/refs/tags/n8.1.3.tar.gz",
        "sha256": "09f990289327d3ebfedf4fc15cae3f884a65dab7a5aac7bbb5b01ff83900efd9",
        "tag_object": "23151b11c75aa44d9ab8db796a53c76acf00f6c0",
    },
    "pyav": {
        "filename": "av-17.1.0.tar.gz",
        "url": "https://files.pythonhosted.org/packages/5e/e3/477fa20578c284abeda08d91b63ee9abaebc93445d8feeb989d3d444bae1/av-17.1.0.tar.gz",
        "sha256": "7f1e71ff621b66253333926f948e00faae11d855b2442133c65128bca64cdeb3",
    },
    "make": {
        "filename": "make-4.4.1-3-x86_64.pkg.tar.zst",
        "url": "https://repo.msys2.org/msys/x86_64/make-4.4.1-3-x86_64.pkg.tar.zst",
        "sha256": "af0bdba17f06fe037f0194069adaa31a8fe45f1a11381501896aea1fae37bd5d",
    },
}
FLAGS = [
    "--toolchain=msvc", "--arch=x86_64", "--target-os=win64",
    "--disable-autodetect", "--disable-everything", "--disable-programs",
    "--disable-doc", "--disable-network", "--enable-shared", "--disable-static",
    "--disable-x86asm", "--disable-debug", "--prefix=../sdk",
]
DLL_PREFIXES = {
    "avcodec-62", "avdevice-62", "avfilter-11", "avformat-62", "avutil-60",
    "swresample-6", "swscale-9",
}
RELINK = """Minimal FFmpeg / PyAV replacement and rebuilding

The application uses seven dynamically linked FFmpeg libraries under LGPL2.1
or later. No GPL/nonfree codecs or external media libraries are enabled.
The exact, unmodified FFmpeg8.1.3 source archive, PyAV17.1 source archive,
configure flags and build script are supplied beside this file. The only
PyAV source change is its version string, identifying this custom build.

To rebuild in a fresh Windows x64 virtual environment with MSVC and Git:
  python -m pip install --only-binary=:all: setuptools==84.2.1 Cython==3.2.9 wheel==0.48.0 delvewheel==1.13.2
  python build_ice_native.py --cache-dir . --output-dir rebuilt --install

Place the supplied source archives in the cache directory using their original
names listed in native-build-inputs.json. The build script verifies all input
SHA256 values and downloads the small GNUmake tool if it is absent. It installs
nothing globally. Changing the build script/FFmpeg source intentionally requires
updating its input digest checks; do not substitute a mismatched binary silently.

To use modified, ABI-compatible FFmpeg libraries with this particular frozen
application, retain the seven existing DLL filenames in _internal/av.libs/.
PyInstaller's folder build allows these shared libraries to be replaced. The
filenames include delvewheel hashes to avoid collisions, so copy your replacement
DLLs to those existing names and update their cross-library import names to match
using delvewheel, or rebuild PyAV and the frozen application with this build
pipeline. Nothing verifies these diagnostic DLL hashes when the application
starts. The published manifest records the original build for comparison; a
modified copy is expected to have different hashes. Reverse engineering needed
to debug your modifications to these LGPL libraries is permitted.

Full license text is in FFmpeg-COPYING.LGPLv2.1.txt. The complete source includes
its copyright notices and LGPL/BSD components. Use the source/configuration for
this exact build; the stock PyPI PyAV wheel has a different dependency closure.
"""


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def run(command, *, cwd, env=None, log=None, timeout=600):
    result = subprocess.run(
        command, cwd=cwd, env=env, capture_output=log is None,
        stdout=None if log is None else log, stderr=None if log is None else subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(f"Native build command failed ({result.returncode}); inspect build logs")
    return result.stdout


def fetch_inputs(cache):
    cache.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, item in INPUTS.items():
        path = cache / item["filename"]
        if not path.exists():
            # Windows Schannel trust works independently of Python's bundled CA
            # store. Verification remains enabled; no TLS bypass is permitted.
            run([str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "curl.exe"),
                 "--fail", "--location", "--silent", "--show-error", "--max-time", "300",
                 "--proto", "=https", "--proto-redir", "=https",
                 "--output", str(path), item["url"]], cwd=cache, timeout=310)
        if digest(path) != item["sha256"]:
            raise RuntimeError(f"Pinned native source/tool digest mismatch: {item['filename']}")
        paths[name] = path
    return paths


def extract_source(archive, destination):
    destination.mkdir()
    with tarfile.open(archive) as source:
        for item in source.getmembers():
            resolved = (destination / item.name).resolve()
            if not resolved.is_relative_to(destination.resolve()) or item.issym() or item.islnk():
                raise RuntimeError("Unexpected native source archive path/link")
        # The pinned archives contain ordinary files/directories only.
        source.extractall(destination, filter="data")
    entries = list(destination.iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        raise RuntimeError("Unexpected native source archive layout")
    return entries[0]


def compiler_environment():
    vswhere = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    installed = subprocess.check_output(
        [str(vswhere), "-latest", "-products", "*", "-requires",
         "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
        text=True, encoding="utf-8").strip()
    batch = Path(installed) / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    if not batch.is_file() or any(char in str(batch) for char in '%!^&|<>"\r\n'):
        raise RuntimeError("No usable installed MSVC x64 build environment")
    command = f'cmd.exe /d /s /c ""{batch}" >nul && set"'
    output = subprocess.check_output(command, text=True, encoding="utf-8", errors="replace")
    environment = {key.upper(): value for key, value in os.environ.items()}
    environment.update((key.upper(), value) for key, value in (
        line.split("=", 1) for line in output.splitlines() if "=" in line and not line.startswith("=")))
    environment.update(DISTUTILS_USE_SDK="1", MSSDK="1")
    environment.pop("MSYS2_ARG_CONV_EXCL", None)
    return environment


def check_ffmpeg(sdk):
    binaries = {path.stem for path in (sdk / "bin").glob("*.dll")}
    if binaries != DLL_PREFIXES:
        raise RuntimeError("Minimal FFmpeg native library set is unexpected")
    directory = os.add_dll_directory(str(sdk / "bin"))
    try:
        native = ctypes.CDLL(str(sdk / "bin" / "avutil-60.dll"))
        native.avutil_license.restype = ctypes.c_char_p
        native.avutil_configuration.restype = ctypes.c_char_p
        license_text = native.avutil_license().decode("utf-8")
        configuration = native.avutil_configuration().decode("utf-8")
    finally:
        directory.close()
    if license_text != "LGPL version 2.1 or later" or configuration.split() != FLAGS:
        raise RuntimeError("Minimal FFmpeg license/configuration disagrees with the reviewed build")
    return {"license": license_text, "configuration": configuration}


def add_provenance(wheel, output, additions):
    """Add source/notice evidence and repair the wheel RECORD without installing."""
    with zipfile.ZipFile(wheel) as archive:
        distinfo = next(name.rsplit("/", 1)[0] for name in archive.namelist()
                        if name.endswith(".dist-info/RECORD"))
        record = distinfo + "/RECORD"
        entries = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/") and name != record}
    for relative, data in additions.items():
        entries[distinfo + "/" + relative] = data
    rows = []
    for name, data in sorted(entries.items()):
        checksum = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
        rows.append((name, "sha256=" + checksum, str(len(data))))
    rows.append((record, "", ""))
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    entries[record] = buffer.getvalue().encode("utf-8")
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in sorted(entries.items()):
            archive.writestr(name, data)
    return distinfo


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "build" / "ice-native-inputs")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist" / "ice-native")
    parser.add_argument("--result-json", type=Path)
    parser.add_argument("--install", action="store_true", help="Install only into this interpreter environment")
    args = parser.parse_args(argv)
    if sys.platform != "win32" or sys.maxsize <= 2**32:
        parser.error("Use Windows x64 Python for this explicit native build")
    if sys.prefix == sys.base_prefix:
        parser.error("Use an isolated virtual environment; global Python is not modified")
    if args.result_json is not None and args.result_json.exists():
        parser.error("--result-json already exists; use a fresh path")
    expected_tools = {"Cython": "3.2.9", "setuptools": "84.2.1", "wheel": "0.48.0", "delvewheel": "1.13.2"}
    versions = {name: importlib.metadata.version(name) for name in expected_tools}
    if versions != expected_tools:
        parser.error("Install the exact isolated build tools listed in the native relink instructions")
    inputs = fetch_inputs(args.cache_dir.expanduser().resolve())
    identity = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    work = ROOT / "build" / "ice-native" / identity
    work.mkdir(parents=True, exist_ok=False)
    output = args.output_dir.expanduser().resolve() / identity
    output.mkdir(parents=True, exist_ok=False)
    compiler = compiler_environment()
    environment = dict(compiler)
    bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git" / "usr" / "bin" / "bash.exe"
    if not bash.is_file():
        raise RuntimeError("Installed Git Bash is required; no global install is attempted")
    make = work / "make"
    make.mkdir()
    run([str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "tar.exe"),
         "-xf", str(inputs["make"]), "-C", str(make)], cwd=work)
    environment["PATH"] = os.pathsep.join((str(make / "usr" / "bin"), str(bash.parent), environment["PATH"]))
    ffmpeg_source = extract_source(inputs["ffmpeg"], work / "ffmpeg-source")
    ffmpeg_build = work / "ffmpeg-build"
    ffmpeg_build.mkdir()
    # FLAGS uses a relative SDK destination so binary configuration strings do
    # not disclose the workstation account or absolute source checkout path.
    with (work / "ffmpeg-configure.log").open("w", encoding="utf-8") as log:
        run([str(bash), str(ffmpeg_source / "configure"), *FLAGS], cwd=ffmpeg_build,
            env=environment, log=log, timeout=300)
    with (work / "ffmpeg-make.log").open("w", encoding="utf-8") as log:
        run([str(bash), "-c", "make -j 8 && make install"], cwd=ffmpeg_build,
            env=environment, log=log, timeout=600)
    sdk = work / "sdk"
    native_check = check_ffmpeg(sdk)
    for library in (sdk / "bin").glob("*.lib"):
        shutil.copy2(library, sdk / "lib" / library.name)
    # Git supplies a different link.exe. It belongs in the shell build PATH,
    # while setuptools must discover the MSVC linker from the original PATH.
    environment = compiler
    pyav = extract_source(inputs["pyav"], work / "pyav-source")
    version_file = pyav / "av" / "about.py"
    original = version_file.read_text(encoding="utf-8")
    if original.strip() != '__version__ = "17.1.0"':
        raise RuntimeError("Unexpected PyAV version source")
    version_file.write_text(f'__version__ = "{VERSION}"\n', encoding="utf-8")
    with (work / "pyav-build.log").open("w", encoding="utf-8") as log:
        run([sys.executable, "setup.py", f"--ffmpeg-dir={sdk}", "bdist_wheel"],
            cwd=pyav, env=environment, log=log, timeout=600)
    unrepaired, = (pyav / "dist").glob("*.whl")
    repaired = work / "repaired"
    repaired.mkdir()
    with (work / "delvewheel.log").open("w", encoding="utf-8") as log:
        run([sys.executable, "-m", "delvewheel", "repair", "--add-path", str(sdk / "bin"),
             "--wheel-dir", str(repaired), str(unrepaired)], cwd=work, env=environment,
            log=log, timeout=120)
    repaired_wheel, = repaired.glob("*.whl")
    with zipfile.ZipFile(repaired_wheel) as archive:
        native_files = {name: hashlib.sha256(archive.read(name)).hexdigest()
                        for name in archive.namelist() if Path(name).suffix.lower() in (".pyd", ".dll")}
    dlls = {Path(name).name.rsplit("-", 1)[0] for name in native_files if name.endswith(".dll")}
    if dlls != DLL_PREFIXES:
        raise RuntimeError("Repaired PyAV wheel has an unexpected DLL dependency closure")
    provenance = {
        "schema": "holdem-minimal-pyav-v1", "version": VERSION,
        "distribution_status": "minimal-lgpl-source-bundled",
        "inputs": INPUTS, "build_tool_versions": versions,
        "msvc_tools_version": environment.get("VCTOOLSVERSION"),
        "windows_sdk_version": environment.get("WINDOWSSDKVERSION"),
        "ffmpeg_flags": FLAGS, "ffmpeg_check": native_check,
        "ffmpeg_source_modifications": [],
        "pyav_source_modifications": [{"file": "av/about.py", "version": VERSION}],
        "native_files_sha256": native_files,
    }
    additions = {
        "ice-minimal-provenance.json": (json.dumps(provenance, indent=2) + "\n").encode(),
        "minimal-sources/native-build-inputs.json": (json.dumps(INPUTS, indent=2) + "\n").encode(),
        "minimal-sources/FFmpeg-configure-flags.json": (json.dumps(FLAGS, indent=2) + "\n").encode(),
        "minimal-sources/RELINK.txt": RELINK.encode(),
        "minimal-sources/build_ice_native.py": Path(__file__).read_bytes(),
        "minimal-sources/FFmpeg-COPYING.LGPLv2.1.txt": (ffmpeg_source / "COPYING.LGPLv2.1").read_bytes(),
        "minimal-sources/FFmpeg-LICENSE.md": (ffmpeg_source / "LICENSE.md").read_bytes(),
        "minimal-sources/" + INPUTS["ffmpeg"]["filename"]: inputs["ffmpeg"].read_bytes(),
        "minimal-sources/" + INPUTS["pyav"]["filename"]: inputs["pyav"].read_bytes(),
    }
    provenance["source_bundle_files_sha256"] = {
        name: hashlib.sha256(data).hexdigest() for name, data in additions.items()
        if name != "ice-minimal-provenance.json"}
    additions["ice-minimal-provenance.json"] = (json.dumps(provenance, indent=2) + "\n").encode()
    wheel = output / repaired_wheel.name
    add_provenance(repaired_wheel, wheel, additions)
    manifest = {"schema": "holdem-minimal-native-build-v1", "wheel_filename": wheel.name,
                "wheel_sha256": digest(wheel), "provenance": provenance}
    manifest_file = output / "native-build-manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    result = {"wheel": str(wheel), "wheel_sha256": manifest["wheel_sha256"],
              "manifest": str(manifest_file), "manifest_sha256": digest(manifest_file),
              "build_logs": str(work)}
    if args.install:
        run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps",
             "--force-reinstall", str(wheel)], cwd=work, timeout=120)
    if args.result_json is not None:
        args.result_json.parent.mkdir(parents=True, exist_ok=True)
        with args.result_json.open("x", encoding="utf-8") as target:
            target.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
