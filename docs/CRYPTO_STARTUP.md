# Explicit crypto startup

Importing `holdem.p2p` modules does not discover or load libsodium, read host
configuration, obtain entropy, or perform native arithmetic. Importing the
desktop adapter `holdem.native_crypto` is inert too. A lobby, a card lookup,
or solo Tk practice can exist without a native crypto runtime.

The embedding host supplies crypto before starting a mental-poker hand.
Desktop entry points use:

```python
from holdem.native_crypto import initialize

initialize()
# Only now create multiplayer sessions, listeners, or workers.
```

The sidecar initializes before constructing its sessions or server. Tk host
and join flows initialize before loading identity files or opening network
connections, and display startup failures in the multiplayer dialog. Test
peer processes and crypto benchmarks initialize in their entry points.

## Core capability

An alternative host can supply an initialized object implementing the raw
byte operations in `holdem.p2p.ristretto.Backend`:

```python
from holdem.p2p import ristretto

ristretto.initialize(backend)
```

The core validates that all required operations are callable and publishes
the capability once. Reinstalling the same object is idempotent; replacing
an installed backend is forbidden. Backends are trusted host capabilities
and must preserve canonical point validation and scalar/group semantics.

Arithmetic before startup raises `CryptoNotInitialized`, including zero and
empty arithmetic shortcuts. It never invokes discovery as a fallback.
`require_initialized()` checks readiness without doing arithmetic. Session
hand entry points check it before changing hand state or loading a lazy
master secret. Constructors and fixed byte constants remain usable before
startup; validating a point requires the backend.

## Native adapter lifecycle

The adapter owns ctypes, discovery and ABI configuration. Its search order
is documented in [native/README.md](../native/README.md). It checks every
required export, configures signatures, calls `sodium_init`, verifies the
32-byte point/scalar and 64-byte hash/wide-scalar sizes, and decodes the
version before publishing the backend.

Concurrent startup calls serialize and share one successful capability.
Failed startup publishes nothing, so a later explicit call can retry.
`NativeCryptoUnavailable` identifies a missing, unloadable, or unsupported
library. `NativeCryptoInitializationError` identifies a present library that
fails initialization or its ABI contract. Unexpected programming errors are
not converted into optional missing-library status.

The base point, 52 card points, Bayer–Groth mask generator and negative-one
scalar are fixed canonical encodings. Tests recompute their original
derivations after explicit startup, preserving the existing deck and wire
bytes without import-time native work.

## Validation policy

Crypto test modules explicitly request startup through `tests/crypto_gate.py`.
Known library absence can skip optional suites locally. CI requires crypto
by default; `HOLDEM_REQUIRE_CRYPTO` explicitly overrides that requirement.
Required absence produces a named gate test failure while unrelated tests
still run. Broken initialization and unexpected errors fail even when
crypto is optional.

Cold subprocess tests block native loading, host paths, entropy and host
adapters while importing every core module. Other controls cover forgotten
startup, thread races, failed initialization and retry, ABI checks, and
compatibility of fixed encodings and primitive operations.
