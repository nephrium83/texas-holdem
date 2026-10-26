# Direct Internet poker: host adapter and acceptance

`DirectPokerRoom` is an optional asyncio host adapter over the existing
peer-verified `Session`. It retains capability admission, full identity pins,
signed envelopes, Bayer–Groth dealing, clock agreement and the casual departure
policy. It does not copy poker rules or turn the hosting player into a trusted
cash-game authority. LAN uses its existing TCP adapter. The reusable base SDK
does not acquire an Internet dependency.

The containing game owns presentation, identity, secrets, setup authentication
and the asyncio loop. Construct the room on that loop with `is_host`, an
`Identity`, a private 32-byte master secret, player nickname, private 16-byte
admission capability, 8-byte discovery token and the full 32-byte host public
key. The host shares its random invitation capability privately. Do not send it
to an introduction service or log it.

After verifying signed connection setup and opening a `DirectPeer`, attach it
with `await room.attach_peer(peer, remote_key=full_verified_remote_key)`. This
takes ownership of that peer. A joiner attaches only its pinned host; the host
has a separate channel for each guest and forwards original signed messages.
Attaching an unauthenticated key supplied by the network violates the caller's
setup contract. Normal poker admission is still required after channel setup.

Await `room.lobby()` for admission and clock readiness, `room.set_ready(True)`
to mark the local player ready, `room.command("start_game")` on the ready host,
and `room.view()` / `room.command(...)` for the existing public player API.
The optional `on_change` callback is a notification on the serialized protocol
worker; GUI hosts should marshal it to their own event loop. Session callbacks,
commands and cryptographic work stay on one worker, outside the network loop.

Always `await room.aclose()` when leaving or disposing the containing scene.
The adapter closes its channels, admission timers, reliability timer and callback
worker; it never stops the containing game's asyncio loop. Failed caller-side
attachments remain the caller's cleanup responsibility. An admitted host loss
ends the guest table visibly with the last agreed balances. Guest departure
uses the existing casual settlement and lets surviving players continue.

Each channel has a serial send pump bounded to 256 pending envelopes and 1 MiB;
the incoming worker has separate byte/count bounds. Full-table proof/decryption
bursts pass through this FIFO without exceeding the native peer's send limit.
Malformed signatures, stale clocks, unauthenticated forwarding, queue overflow
and missing heartbeats close the offending connection. Raced sends after an
ordinary disconnect are inert. Cleanup errors cannot produce a passing proof.

## Engineering package

The separate `DirectConnectProbe.exe` supports byte delivery and real poker
acceptance. It is not the player-facing Internet Host/Join screen. Its direct
peer stack configures STUN and reliable ordered UDP channels, with no TURN.
The one-folder package includes the existing minimal LGPL native runtime and
pinned official libsodium1.0.22 x64 with ISC/source notices. Frozen poker selects
only its bundled DLL; missing it fails even if a developer copy exists elsewhere.

Build from an isolated Windows environment with the reviewed minimal PyAV
runtime already installed:

```powershell
python tools/prepare_ice_sodium.py --output-dir build/sodium-new --result build/sodium-new.json
python tools/build_ice_probe.py --sodium-result build/sodium-new.json --self-test --poker-self-test --result-json build/probe-new.json
```

The result names a new ZIP and exact manifest. `--self-test` transfers 4 KiB,
32 KiB and 512 KiB messages locally. `--poker-self-test` runs two independent
own-seat drivers through ten real hands, verifies two shuffle proofs per hand,
conserves all chips and compares signed settled balance receipts. It verifies
resource shutdown as well as gameplay. Three-player/ten-hand, nine-player hand,
wrong capability/pin, disconnect, signature and queue cases run in source tests.

For separate-network acceptance, manually dispatch `direct-ice-proof.yml` with
an immutable source SHA, fresh public proof keys/nonce and `game_hands: '10'`.
The controlled engineering host obtains the exact runner-built ZIP and checks
every manifest hash. Encrypted signed setup binds the run, source, package,
nonce and peer identities. A private two-seat engineering capability is derived
from pinned X25519 keys; public workflow inputs do not reveal it. The temporary
mailbox is removed before gameplay. Both native exits, ten per-hand public
balance vectors, direct UDP route and normal cleanup must pass.

Home-to-cloud evidence proves that route. It does not establish compatibility
with every NAT/firewall pair. No router mapping is part of this path.

## Automatic introductions and the desktop

The desktop now offers Internet Host/Join alongside LAN and local bots. The
containing desktop supplies its existing asyncio loop to `InternetTable`;
its asynchronous `InternetSession` composes `DirectPokerRoom` after signed,
encrypted setup. The synchronous facade is used only by off-loop desktop
callbacks. Leave cancels pending setup before disposing the room.

Internet invitations use explicit version3 and the `direct-udp-v1` profile.
They carry a trusted service identifier, full host pin, routing token, room
generation, expiry and private capability. The identifier selects a configured
WSS origin; an invite cannot choose a URL. The desktop's configured default is
`wss://poker.errantsaints.space/v1`; `HOLDEM_INTRODUCTION_URL` lets a containing
application select its own trusted WSS `/v1` endpoint. Plain WebSockets are
confined to explicitly injected literal-loopback acceptance fixtures.

The introduction service authenticates one-use socket challenges and signed
room/guest records, then forwards one encrypted offer and answer per attempt.
The host checks a capability proof before allocating a native peer. Capabilities
and complete invites never reach the service. It does not run a Session or carry
gameplay. A successful Start closes setup only after the serialized game core
accepts readiness, excluding new attachments during that transition.

The service has room/socket/challenge/attempt/rate/expiry limits and bounded
JSON/queues. Its retained-byte counter covers records and outgoing messages;
native WebSocket input buffers are additional, so deployment also limits the
container's memory/CPU and may lower service capacities. Trusted proxy headers
are accepted only from exact configured proxy IPs. See
[`deploy/introduction/README.md`](../deploy/introduction/README.md).

Acceptance covers three independent clients joining from one invite, ten
matching real hands after the service stops, wrong capability/pin, replay,
expiry, malformed input, Leave, readiness races and cleanup faults. A separate
three-process Godot test drives the actual Internet buttons and verifies normal
launcher/child exit. Final public-route and frozen desktop evidence is recorded
per delivered source/package; implementation alone is not a completed release.
