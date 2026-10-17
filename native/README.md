# Native crypto libraries

The mental-poker shuffle (`docs/MULTIPLAYER.md` Phase 2) uses
**Ristretto255** elliptic-curve operations from **libsodium**, accessed
from Python via the explicit desktop adapter `holdem/native_crypto.py`
(ctypes). The protocol uses the backend through `holdem/p2p/ristretto.py`.

The compiled libsodium shared library is a **build artifact**, not
source, so it is **git-ignored** (see `.gitignore`). Each machine builds
or drops its own copy here. Calling `holdem.native_crypto.initialize()`
searches for the library in this order; importing core modules or the host
adapter itself does not load or initialize it:

1. `$HOLDEM_LIBSODIUM` — explicit full path override
2. this `native/` directory (`libsodium.dll` / `.so` / `.dylib`)
3. the dev build output under `~/poker-native/...`
4. the OS loader path (PATH / ldconfig)

Put a platform-appropriate libsodium **with the Ristretto255 API
enabled** here and explicit host startup will find it. libsodium's standard build
includes that API; PyNaCl's bundled copy does **not** expose it, so a
generic `pip install pynacl` is not sufficient.

## Building libsodium on Windows (MSVC)

Requires Visual Studio Build Tools with the C++ workload (any recent
version). From a clone of https://github.com/jedisct1/libsodium:

```bat
call "<VS>\VC\Auxiliary\Build\vcvars64.bat"
cd builds\msvc\vs2022
msbuild libsodium.sln ^
  /p:Configuration=DynRelease /p:Platform=x64 ^
  /p:WindowsTargetPlatformVersion=10.0.26100.0 ^
  /p:PlatformToolset=v145 ^
  /p:UseEnv=true
```

`/p:UseEnv=true` is required: it makes MSBuild honor the `INCLUDE`/`LIB`
paths that `vcvars64.bat` set, so the CRT/SDK headers resolve. Without
it the build fails with "cannot open include file 'stddef.h'".

The DLL lands at
`bin\x64\Release\v145\dynamic\libsodium.dll`; copy it here as
`native/libsodium.dll`.

## Building on Linux / macOS

```sh
git clone https://github.com/jedisct1/libsodium
cd libsodium && ./configure && make -j
# copy src/libsodium/.libs/libsodium.so (or .dylib) into native/
```

## Verifying

An embedding desktop process initializes crypto before starting hands:

```python
from holdem.native_crypto import initialize
initialize()
```

The sidecar and multiplayer onboarding call this before creating sessions
or opening a listener. Solo Tk practice does not require the native library.
Arithmetic before startup raises `CryptoNotInitialized`; it never triggers
library discovery implicitly. See `docs/CRYPTO_STARTUP.md`.

```sh
python -m pytest tests/test_ristretto.py -q
```

If libsodium is not found, those tests **skip** (they do not fail), so
the rest of the suite still runs on machines without the native library.

## When a skip is not allowed

Crypto-dependent modules skip themselves this way, and libsodium is a
git-ignored build artifact, so its absence is invisible in the result: the
shuffle-soundness, DLEQ, deal and audit suites vanish and the run still
reports success. `tests/crypto_gate.py` decides whether that is allowed
here, and `tests/test_crypto_gate.py` enforces it.

Every run prints its posture, in the run header and again in the terminal
summary — the second one because CI runs `pytest -q`, where the header is
suppressed. Copy the wording from `tests/crypto_gate.py`, not from here:

```
crypto: libsodium 1.0.20 — crypto-gated suites RUN
crypto: UNAVAILABLE — crypto-gated suites SKIP, and that is permitted here (set HOLDEM_REQUIRE_CRYPTO=1 to make it a failure). ...
crypto: UNAVAILABLE and REQUIRED here — ...
```

The requirement is **on by default on CI** (keyed to `CI`, which the runner
sets itself) and **off** elsewhere. Override it in either direction with
`HOLDEM_REQUIRE_CRYPTO`:

```sh
HOLDEM_REQUIRE_CRYPTO=1 pytest -q   # arm it locally: absence fails
HOLDEM_REQUIRE_CRYPTO=0 pytest -q   # waive it: absence skips
```

Accepted values are `1/true/yes/on` and `0/false/no/off`, case-insensitive.
Anything else **raises** rather than defaulting: a mistyped `1` that read as
"not required" would silently drop the whole crypto suite on the machine
where somebody had just armed the gate — the exact failure this gate exists
to remove. Set-but-empty (`HOLDEM_REQUIRE_CRYPTO= pytest`) is treated as
unset, since that is a shell neutralising a variable rather than a typo, and
the `CI` default decides again.

The refusal surfaces as a named test failure from
`tests/test_crypto_gate.py`, plus a `crypto: GATE MISCONFIGURED` line in the
log. It is deliberately not raised from the reporting hooks themselves,
which would replace the results of every test that ran with an
INTERNALERROR.

A present but broken library or an unexpected startup exception always fails;
only known library absence is eligible for an optional skip. Startup is explicit
in the test gate and each crypto-dependent harness.
