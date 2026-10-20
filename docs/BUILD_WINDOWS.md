# Windows preview build

The preview bundles the Python sidecar, libsodium, Godot runtime, and game
resources. Extract the entire ZIP and launch `Poker.exe`; the destination
machine does not need Python, Godot, a checkout, or a configured DLL path.
The preview is a local game against bots. LAN Host/Join is a later release.
The packaged preview targets 64-bit Windows 10 or newer.

Build on Windows with Python 3.10 or newer, the project dependencies, and
PyInstaller 6 installed:

```powershell
python -m pip install -e . pyinstaller
python tools/build_windows.py --godot "C:/path/to/Godot.exe" --libsodium "C:/path/to/libsodium.dll" --self-test
```

The DLL must expose Ristretto255 and have its `LICENSE` beside it or in a
parent directory. Both inputs are explicit; the distributable stores no
developer-specific runtime paths. `--output-dir` selects a parent directory
for a fresh timestamped build folder and ZIP. Earlier builds are preserved.

The build stages the Godot project without GUT, unit tests, or editor caches,
then uses `--export-pack`. It bundles the supplied Godot executable as the
runtime, launched with `--main-pack`; export templates are not needed.
The real-client headless driver remains packed for `Poker.exe --self-test`.

The ZIP has `Poker.exe` at its root. Keep its `_internal` directory beside it.
`_internal/assets/` holds `PokerRuntime.exe` and `poker.pck`;
`_internal/native/` holds libsodium. `licenses/` includes the project, Python,
native runtime, and bundled Python dependency notices. Godot's own built-in
license and third-party notices are extracted from the exact shipped runtime.

The build prints the artifact location and ZIP SHA-256. `build-manifest.json`
records source HEAD, working-tree status, source hashes, tool versions,
input hashes, packaged file hashes, and the optional self-test result.
Build logs remain under `build/windows-preview/` for troubleshooting.
With `--self-test`, no ZIP is created unless the frozen application completes
the real ten-hand Godot check with only Windows system directories on PATH
and an isolated config directory. Its detailed logs stay in the build folder.

Before sharing a preview, run its real ten-hand self-test, then manually
check normal launch, betting, next hand, match completion, and clean exit.
Also test an extracted ZIP from an unrelated folder, ideally on a clean
Windows account or machine. A local package self-test does not establish
LAN functionality or clean-machine compatibility.

`Poker.exe --self-test-visible --log-dir <folder>` runs the same ten-hand
check in a rendered window and saves `table.png` alongside its reports.
