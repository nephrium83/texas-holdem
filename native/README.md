# Native crypto libraries

The mental-poker shuffle (`docs/MULTIPLAYER.md` Phase 2) uses
**Ristretto255** elliptic-curve operations from **libsodium**, accessed
from Python via `holdem/p2p/ristretto.py` (ctypes).

The compiled libsodium shared library is a **build artifact**, not
source, so it is **git-ignored** (see `.gitignore`). Each machine builds
or drops its own copy here. `holdem/p2p/ristretto.py` searches for the
library in this order:

1. `$HOLDEM_LIBSODIUM` — explicit full path override
2. this `native/` directory (`libsodium.dll` / `.so` / `.dylib`)
3. the dev build output under `~/poker-native/...`
4. the OS loader path (PATH / ldconfig)

Put a platform-appropriate libsodium **with the Ristretto255 API
enabled** here and the wrapper will find it. libsodium's standard build
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

```sh
python -m pytest tests/test_ristretto.py -q
```

If libsodium is not found, those tests **skip** (they do not fail), so
the rest of the suite still runs on machines without the native library.

## When a skip is not allowed

Roughly twenty modules skip themselves this way, and libsodium is a
git-ignored build artifact, so its absence is invisible in the result: the
shuffle-soundness, DLEQ, deal and audit suites vanish and the run still
reports success. `tests/crypto_gate.py` decides whether that is allowed
here, and `tests/test_crypto_gate.py` enforces it.

Every run prints its posture in the pytest header, e.g.

```
libsodium: present (libsodium 1.0.20) -- required here: CI runner (GITHUB_ACTIONS, CI)
libsodium: ABSENT (...) -- not required here: developer machine (...)
```

The requirement is **on by default on CI** (keyed to `GITHUB_ACTIONS` /
`CI`, which the runner sets itself) and **off** elsewhere. Override it in
either direction with `HOLDEM_REQUIRE_LIBSODIUM`:

```sh
HOLDEM_REQUIRE_LIBSODIUM=1 pytest -q   # arm it locally: absence fails
HOLDEM_REQUIRE_LIBSODIUM=0 pytest -q   # waive it: absence skips
```

An unrecognised value raises rather than defaulting, because a typo'd
waiver that read as "not required" would silently drop the whole crypto
suite — the exact failure this gate exists to remove.
