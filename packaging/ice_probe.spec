# Separate engineering probe; does not alter the shipped Godot desktop build.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

root = Path(os.environ["POKER_PROJECT_ROOT"])
datas, binaries, av_imports = collect_all("av")
datas += copy_metadata("aiortc", recursive=True)
hiddenimports = av_imports + collect_submodules("google_crc32c") + collect_submodules("dns")
hiddenimports += ["holdem.ice_poker_probe", "holdem.ice_room", "holdem.ice_transport"]
sodium = Path(os.environ["POKER_ICE_SODIUM_DLL"]).resolve(strict=True)
if not sodium.is_file() or sodium.name != "libsodium.dll":
    raise RuntimeError("The verified libsodium DLL is required for this probe")
binaries += [(str(sodium), "native")]
a = Analysis(
    [str(root / "packaging" / "ice_probe_entrypoint.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=[
        "holdem.gui", "holdem.onboarding", "holdem.audio",
        "holdem.desktop_launcher", "holdem.godot_sidecar",
        "tkinter", "PIL", "numpy", "pygame", "pytest",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="DirectConnectProbe", debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False, console=True,
    contents_directory="_internal",
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False, name="DirectConnectProbe",
)
