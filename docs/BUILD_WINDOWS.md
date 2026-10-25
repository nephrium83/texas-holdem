# Windows preview build

The preview bundles the Python sidecar, libsodium, Godot runtime, and game
resources. Extract the entire ZIP and launch `Poker.exe`; the destination
machine does not need Python, Godot, a checkout, or a configured DLL path.
The desktop build includes local bots, authenticated LAN Host/Join and direct
Internet Host/Join through a trusted configured introduction service. See
[`LAN_PLAY.md`](LAN_PLAY.md) for the one-invite join and Ready flow.
The packaged preview targets 64-bit Windows 10 or newer.

Build with 64-bit Windows Python 3.13 and PyInstaller 6.20.0. First install
the reviewed minimal PyAV runtime produced by `tools/build_ice_native.py`;
that explicit source build requires MSVC and Git tools. Stock PyPI PyAV
wheels are refused because their native redistribution closure differs.
The native source-build helper and engineering workflow record the pinned
source hashes, disabled media features, LGPL notices and relink instructions.
With that reviewed runtime installed in an isolated build environment:

```powershell
python -m pip install --only-binary=:all: -c packaging/ice-probe-constraints.txt ".[internet]" pyinstaller==6.20.0
python tools/prepare_ice_sodium.py --output-dir build/desktop-sodium --result build/desktop-sodium.json
python tools/build_windows.py --godot "C:/path/to/Godot.exe" --sodium-result build/desktop-sodium.json --self-test --internet-self-test --result-json build/desktop-result.json
```

The sodium helper verifies the official libsodium 1.0.22 MSVC archive,
selected x64 DLL, Ristretto255 ABI and exact ISC notice. Its portable source
manifest and attribution are bundled unchanged. An optional `--libsodium`
argument is accepted only when its bytes match this verified input; an
arbitrary developer DLL or a neighboring license is insufficient.
`--output-dir` selects a parent directory
for a fresh timestamped build folder and ZIP. Earlier builds are preserved.

The build stages the Godot project without GUT, unit tests, or editor caches,
then uses `--export-pack`. It bundles the supplied Godot executable as the
runtime, launched with `--main-pack`; export templates are not needed.
The real-client headless driver remains packed for `Poker.exe --self-test`.

The ZIP has `Poker.exe` at its root. Keep its `_internal` directory beside it.
`_internal/assets/` holds `PokerRuntime.exe` and `poker.pck`;
`_internal/native/` holds libsodium. `licenses/` includes the project, Python,
native runtime, minimal FFmpeg/PyAV corresponding sources and bundled Python
dependency notices, including websockets 16.1.1. Godot's own built-in
license and third-party notices are extracted from the exact shipped runtime.

The build prints the artifact location and ZIP SHA-256. `build-manifest.json`
records source HEAD, working-tree status, source hashes, tool versions,
input hashes, exact packaged/native file coverage and optional self-test results.
The source snapshot must stay unchanged throughout the build. The manifest
also binds the actual Godot runtime/resource pack and reviewed native bytes;
generic file inventory changes cannot silently replace those inputs.
Build logs remain under `build/windows-preview/` for troubleshooting.
With `--self-test`, no ZIP is created unless the frozen application completes
the real ten-hand Godot check with only Windows system directories on PATH
and an isolated config directory. Its detailed logs stay in the build folder.
`--internet-self-test` additionally requires the frozen direct poker runtime
to complete ten real local hands with matching balances, Bayer–Groth proofs
and no remaining peers, callback workers or tasks. It runs without STUN and
does not establish Internet reachability. Neither check installs a preview
or changes router/firewall settings.

Before sharing a preview, run its real ten-hand self-test, then manually
check normal launch, betting, next hand, match completion, and clean exit.
Also test an extracted ZIP from an unrelated folder, ideally on a clean
Windows account or machine. A local package self-test does not establish
LAN/Internet functionality or clean-machine compatibility. Set
`HOLDEM_WINDOWS_EXE` to the built `Poker.exe` and run
`python -m pytest tests/test_windows_package.py -q` for relocation, both
ten-hand checks, visible-window cleanup and missing-bundled-DLL rejection.
The latter must fail even if a valid external DLL override is present.

`Poker.exe --self-test-visible --log-dir <folder>` runs the same ten-hand
check in a rendered window and saves `table.png` alongside its reports.

`Poker.exe --internet-runtime-self-test --log-dir <folder>` performs the local
direct poker runtime check before starting Godot and writes
`internet-runtime-self-test.json`. This checks packaged dependencies and
cleanup; final Internet acceptance still requires real Godot clients on
separate networks using the exact same ZIP.

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

`Poker.exe --internet-self-test-config <config.json> --log-dir <logs>` uses
the same real controls with `network: "internet"` and Internet Host/Join.
`expected_players` selects the expected roster/proof count. For a coordinated
test, each client writes `ready_signal_path` while still on hand 0; the host
waits for `start_signal_path` before pressing Start. This lets the coordinator
stop the setup service before any hand begins, compare every client's ten
settled balances, then release their `finish_signal_path` files for normal
exit. An optional `abort_signal_path` lets the test coordinator end its own
diagnostic without touching another game.

Only this private Internet diagnostic accepts `test_service_endpoint`, and
only as an explicit `ws://<literal-loopback-IP>:<port>/v1` fixture address.
Ordinary play and production service configuration still require verified
WSS. Omit the fixture field for a public-service test; never put an invite,
capability or hole cards in command-line arguments or public reports.
