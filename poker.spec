# poker.spec — PyInstaller single-file build for Texas Hold'em
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# Collect entire packages so DLLs / rust bindings / data are included
datas_c, binaries_c, hiddenimports_c = collect_all('cryptography')
datas_p, binaries_p, hiddenimports_p = collect_all('pygame')

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=binaries_c + binaries_p,
    datas=datas_c + datas_p,
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
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Texas_Holdem',
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
