# PyInstaller 6 onedir build. Paths are supplied by tools/build_windows.py.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

root = Path(os.environ["POKER_PROJECT_ROOT"])
assets = Path(os.environ["POKER_BUILD_ASSETS"])
libsodium = Path(os.environ["POKER_LIBSODIUM"])

a = Analysis(
    [str(root / "packaging" / "windows_entrypoint.py")],
    pathex=[str(root)],
    binaries=[(str(libsodium), "native")],
    datas=[
        (str(assets / "PokerRuntime.exe"), "assets"),
        (str(assets / "poker.pck"), "assets"),
    ],
    hiddenimports=collect_submodules("holdem.p2p"),
    excludes=[
        "holdem.gui", "holdem.onboarding", "holdem.audio",
        "tkinter", "PIL", "numpy", "pygame", "pytest",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Poker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    contents_directory="_internal",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Poker",
)
