# Explicit legacy Tkinter harness build; never the Godot player release.
import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent
# Reuse the release input verifier; never discover an arbitrary developer DLL.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.prepare_ice_sodium import verify_result

receipt = os.environ.get("POKER_SODIUM_RESULT")
if not receipt:
    raise SystemExit(
        "Legacy Tk packaging requires POKER_SODIUM_RESULT from "
        "tools/prepare_ice_sodium.py; see docs/BUILD_WINDOWS.md"
    )
sodium = verify_result(receipt)

# Collect entire packages so DLLs / rust bindings / data are included
datas_c, binaries_c, hiddenimports_c = collect_all('cryptography')
datas_p, binaries_p, hiddenimports_p = collect_all('pygame')

a = Analysis(
    [str(ROOT / 'packaging' / 'legacy_tk_entrypoint.py')],
    pathex=[str(ROOT)],
    binaries=binaries_c + binaries_p + [(sodium["dll"], "native")],
    datas=datas_c + datas_p + [
        (sodium["license"], "licenses"),
        (sodium["attribution"], "licenses"),
        (sodium["manifest"], "."),
    ],
    hiddenimports=(
        hiddenimports_c
        + hiddenimports_p
        + collect_submodules('holdem')
        + [
            'numpy',
            'holdem',
            'holdem.p2p',
            'holdem.p2p.identity',
            'holdem.p2p.transport',
            'holdem.p2p.session',
            'holdem.p2p.wire',
            'holdem.p2p.invite',
            'holdem.p2p.stun',
            'holdem.p2p.shuffle',
            'holdem.audio',
            'holdem.hand_history',
            'holdem.session_stats',
            'holdem.notes',
            'holdem.settings',
            'holdem.onboarding',
            'holdem.engine',
            'holdem.gui',
            'tkinter',
            'tkinter.ttk',
            'tkinter.messagebox',
            'tkinter.simpledialog',
            'tkinter.colorchooser',
        ]
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'scipy', 'pandas', 'PIL', 'cv2', 'test'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Texas_Holdem_Legacy_Tk',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
