# Explicit identity at the core boundary

The protocol receives its signing capability from the application embedding
it. Importing `wire`, `invite`, `join_auth`, or the host's `identity` adapter
does not create or load an identity file. Verifying a signed envelope uses
only the sender's public key.

## Host startup

The desktop host loads its persisted identity explicitly:

```python
from holdem.p2p import identity, wire
from holdem.p2p.timeout import RealClock

local_identity = identity.load_or_create()
clock = RealClock()
raw = wire.pack("chat", {"text": "hello"}, signer=local_identity, clock=clock)
verified = wire.unpack(raw, clock=clock)
```

`load_or_create()` caches one default identity per process. Concurrent first
calls share one initialization. Its file location, JSON keys, key material,
and support for files predating the X25519 companion key remain unchanged.
Importing the adapter is inert; calling the loader is the point where file
access occurs. The module's old signing/key accessors remain lazy host-side
compatibility helpers.

`load_or_create(path)` loads a separate identity at an explicit path without
replacing the process default. A caller that already owns its keys can
instead provide any object implementing `signing.Signer`:

- `public_key_bytes() -> bytes`: the raw 32-byte Ed25519 public key.
- `sign(message: bytes) -> bytes`: its Ed25519 signature over those bytes.

The core does not import the persisted identity adapter. Independent hosts
can supply different signers in one process without changing a global key.

## Explicit inputs

These entry points now require their identity inputs:

| Entry point | Required input |
| --- | --- |
| `wire.pack(type, payload, prev_hash, *, signer, clock)` | Signer and host clock; the existing default `prev_hash` is retained |
| `invite.generate_room_code(host_pubkey, ...)` | Host's full Ed25519 public key |
| `joiner_admission_from_invite(parsed, *, joiner_pubkey)` | Joiner's Ed25519 public key |
| `JoinAuthenticator(..., *, signer, clock)` | Signer matching the admission transcript's joiner key, plus host clock |
| `Session.add_local_player(conn_id, *, public_key, x25519_public_key=b"")` | Local Ed25519 key and optional X25519 public key |

Desktop startup passes the same identity to admission, invite generation,
and signed player information. Callback installation and admission remain
before connection establishment; player information remains after the
authenticated accept. Local registration still passes through Session's
existing owner and immutable signing-key binding.

## Compatibility and scope

Envelope fields, JSON encoding, canonical signature input, hash chaining,
timestamps, and protocol version are unchanged. A fixed legacy envelope
test pins the complete output bytes. Persisted key formats and poker rules
are unchanged.

The identity slice removes the identity write exceptions and four
core-to-identity imports. The following [session dependencies slice](SESSION_DEPENDENCIES.md)
removes Session's transport and device-secret fallbacks and adds explicit
clocks. The invite codec's socket helpers remain the sole exception.

This change starts from `origin/main` at `f3abf49`. Draft PR #52 modifies
Session lifecycle code but has no hunk in `add_local_player`; this slice
leaves the constructor and lifecycle untouched. Its rewritten peer test
helper will need the same explicit signer/public-key arguments when the
branches are combined.

## Validation (2026-10-15)

Validated on BWING with Python 3.13.3, libsodium 1.0.22 and Godot 4.7.1.
The implementation snapshot was committed as `e727a59` before running the
deliberate-break controls.

- Full `python -m pytest -q`, with `HOLDEM_REQUIRE_CRYPTO=1` and `GODOT_BIN`
  set: **1,537 passed, 2 skipped** in 294.35 seconds. The skips are the same
  existing actor-dependent timeout cases seen on the pristine baseline;
  all crypto-gated suites ran.
- Godot GUT (`--headless --path godot -s addons/gut/gut_cmdln.gd
  -gdir=res://test/unit -gexit`): **151 passed** across 13 scripts.
- `python tests/gui_smoke.py`: **15 hands**, **2,400/2,400 chips**, no
  callback exceptions.
- A direct pristine-main/refactor comparison with a fixed signing key,
  payload and timestamp produced identical signed envelope bytes.
- The wrong-signer control triggers
  `test_wire_uses_only_the_requested_signer`; the swapped-host-key control
  triggers
  `test_two_signed_peers_keep_independent_identity_and_write_no_config`.
  Both controls pass by detecting their deliberate break.

The pristine baseline initially lacked Godot's generated class-name cache,
causing two end-to-end parse failures. After `godot --headless --path godot
--import`, both failed cases passed unchanged. The refactored full run used
the same import preparation and passed both end-to-end cases. Generated
tracked import metadata is excluded from this change.
