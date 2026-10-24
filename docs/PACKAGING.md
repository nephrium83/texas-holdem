# Installing poker in another application

The `holdem` distribution includes the rules engine, player-filtered views,
and embedding interface. A base installation has no third-party runtime
dependencies:

```powershell
python -m pip install .
```

The base rules interface runs in a Python host without Godot, Tkinter,
Pillow, cryptography, libsodium, a router library, or a network connection.
Install the built wheel in the host application's Python environment; this
does not require copying or modifying the poker rules.

The same distribution also carries optional desktop and peer-to-peer host
adapters. Their presence in the wheel does not initialize them or install
their dependencies. Select an extra when using those adapters:

| Installation | Purpose | Python dependencies |
|---|---|---|
| `python -m pip install .` | Embed the local poker rules and player views | None |
| `python -m pip install ".[p2p]"` | Use the authenticated peer-to-peer protocol | cryptography |
| `python -m pip install ".[desktop]"` | Run the existing desktop application | Pillow and cryptography |
| `python -m pip install -e ".[dev]"` | Develop and test the entire application | Desktop dependencies, pytest, and wheel build tools |

The peer-to-peer mental-poker protocol additionally requires a compatible
Ristretto-enabled libsodium runtime, configured as described in
[`MULTIPLAYER.md`](MULTIPLAYER.md). The Python extra does not provide that
native runtime. The standalone Godot desktop distribution also needs its
Godot runtime and assets; the Python wheel is not the portable Windows game.
See [`BUILD_WINDOWS.md`](BUILD_WINDOWS.md) for that build.

## Build and verify a wheel

From the checkout:

```powershell
python -m pip install -e ".[dev]"
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
python -m pytest tests/test_core_package.py -q
```

The packaging acceptance test builds a wheel from a temporary source
snapshot, installs it with dependency resolution disabled into a fresh
virtual environment outside the checkout, and imports the installed rules
and embedding interface from an unrelated working directory. It plays a
complete hand through the public table API and verifies a stable close
receipt. It also checks that optional desktop and cryptographic dependencies
were not installed.
Wheel construction and installation in this test use no package index.

One distribution currently contains both the embeddable modules and optional
host code. This keeps imports compatible with existing applications while
making the base installation independent of the desktop dependencies. It
is a Python integration package; games in another language still need a
Python runtime or a separately implemented message bridge.
