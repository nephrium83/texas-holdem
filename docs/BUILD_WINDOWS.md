# Windows preview build

The preview bundles the Python sidecar, libsodium, Godot runtime, and game
resources. Extract the entire ZIP and launch `Poker.exe`; the destination
machine does not need Python, Godot, a checkout, or a configured DLL path.
The preview supports local bots and authenticated LAN Host/Join. See
[`LAN_PLAY.md`](LAN_PLAY.md) for the one-invite join and Ready flow.
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

`Poker.exe --presentation-self-test --log-dir <folder>` separately exercises
the normal Local bots / Start game flow, animated shuffle and two-card deal,
paced bot response, a single All In click, Next Hand, and leaving during a
deal. It requires an unacted opening hand and checks that the old table stays
closed after returning to the menu. The ten-hand check above uses the bare
sidecar and does not establish this desktop flow.

Use `--presentation-self-test-visible` instead to run that check in a rendered
window and save shuffle, dealing, revealed-card, result, and menu frames under
`<folder>/captures/`. The report is `presentation-self-test.json`.

For a paired check on two Windows computers, `Poker.exe
--lan-self-test-config <config.json> --log-dir <logs>` runs the real lobby and
betting controls headlessly. Each local JSON config names `role` (`host` or
`join`), `nickname`, `target_hands`, `report_path`, and either
`invite_write_path` or `invite_read_path`. Transfer the host’s private invite
file to the joining computer without logging its contents. An optional
`finish_signal_path` keeps each app open after writing its result until the
coordinator has compared both reports. Reports include settled stacks and
private-card rendering check counts, but no invites or hole cards.
