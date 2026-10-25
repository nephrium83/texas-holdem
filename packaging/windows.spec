# PyInstaller 6 onedir build. Paths are supplied by tools/build_windows.py.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

root = Path(os.environ["POKER_PROJECT_ROOT"])
assets = Path(os.environ["POKER_BUILD_ASSETS"])
libsodium = Path(os.environ["POKER_LIBSODIUM"])
datas, binaries, av_imports = collect_all("av")
datas += copy_metadata("aiortc", recursive=True) + copy_metadata("websockets")
datas += [
    (str(assets / "PokerRuntime.exe"), "assets"),
    (str(assets / "poker.pck"), "assets"),
]
binaries += [(str(libsodium), "native")]
hiddenimports = collect_submodules("holdem.p2p") + av_imports
hiddenimports += collect_submodules("google_crc32c") + collect_submodules("dns")
hiddenimports += collect_submodules("websockets")
hiddenimports += ["holdem.internet_session", "holdem.introduction_client", "holdem.ice_poker_probe"]

a = Analysis(
    [str(root / "packaging" / "windows_entrypoint.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
