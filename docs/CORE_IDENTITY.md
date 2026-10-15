# Explicit identity at the core boundary

The protocol receives its signing capability from the application embedding
it. Importing `wire`, `invite`, `join_auth`, or the host's `identity` adapter
does not create or load an identity file. Verifying a signed envelope uses
only the sender's public key.

## Host startup

The desktop host loads its persisted identity explicitly:

```python
from holdem.p2p import identity, wire

local_identity = identity.load_or_create()
raw = wire.pack("chat", {"text": "hello"}, signer=local_identity)
verified = wire.unpack(raw)
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
| `wire.pack(type, payload, prev_hash, *, signer)` | Signer; the existing default `prev_hash` is retained |
| `invite.generate_room_code(host_pubkey, ...)` | Host's full Ed25519 public key |
| `joiner_admission_from_invite(parsed, *, joiner_pubkey)` | Joiner's Ed25519 public key |
| `JoinAuthenticator(..., *, signer)` | Signer matching the admission transcript's joiner key |
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

The import-boundary allowlists remove the identity write exceptions and
four core-to-identity imports. The remaining exceptions are the invite
codec's socket helpers and Session's global transport and device-secret
fallbacks; those belong to later slices.

This change starts from `origin/main` at `f3abf49`. Draft PR #52 modifies
Session lifecycle code but has no hunk in `add_local_player`; this slice
leaves the constructor and lifecycle untouched. Its rewritten peer test
helper will need the same explicit signer/public-key arguments when the
branches are combined.
