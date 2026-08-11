# M-8 — peer admission and identity binding

Audit only. No code changed. Covers the **production** path
(`holdem/p2p/transport.py` + `session.py`), not `SimpleTcpTransport`,
which is the sidecar/test transport.

**Verdict: M-8 is (1) lack of admission authentication + (2) a Sybil /
admission-policy problem. It is _not_ (3) protocol impersonation.** The
two properties that would make it impersonation both hold: connection
identity cannot be forged, and message signatures cannot be forged.

But the audit found a separate, unlisted defect — `_on_player_list`
accepts roster mutations from any peer — and a structural one: **the
Ed25519 identity is verified on every message and then used for nothing.**

---

## 1. The five identities

| # | Identity | Where it lives | Lifetime |
|---|---|---|---|
| 1 | **Connection** | `conn_id`, `transport._new_conn_id()` → `uuid.uuid4()` | one TCP connection |
| 2 | **Device** | `device_secret.load_or_create()`, 32 bytes on disk, owner-only | machine |
| 3 | **Signing** | Ed25519 in `identity.py`, persisted; `peer_id()` = pubkey prefix | machine |
| 4 | **Player / seat** | `Player.conn_id`, `Session._seat_order[seat]` — a list of **conn_ids** | one table |
| 5 | **DKG / BG** | seat index, via `keygen_pop` and `MentalDeal(seat=…)` | one hand |

---

## 2. What binds each transition

```
 TCP connection
      │  (a) server-side uuid4, per accepted socket
      ▼
   conn_id  ───────────────────────────────┐
      │  (b) player_info carries pubkey     │
      ▼                                     │ (e) authorization
   pubkey (Ed25519)                         │     uses conn_id
      │  (c) signature verified per message │     ONLY
      ▼                                     │
   Player entry                             │
      │  (d) game_start seat_order          │
      ▼                                     │
    seat  ◀──────────────────────────────────┘
      │  (f) seat index is the DKG/BG participant id
      ▼
 DKG share / BG shuffle round
```

| Edge | Binding | Strength |
|---|---|---|
| (a) | Locally generated UUID, never read from the wire | **Sound.** A peer cannot choose or collide with another's `conn_id` |
| (b) | `_on_player_info` records `peer_id = msg["pubkey"][:16]` | **Recorded, never enforced.** Written once; no later message is checked against it |
| (c) | `wire.unpack` verifies Ed25519 over the canonical pre-image and raises on failure | **Sound but inert** — see §4 |
| (d) | `_on_game_start`, host-only, frozen once `PLAYING` | **Sound** |
| (e) | Every handler authorizes on `conn_id` (`_seat_order[seat] == conn_id`, `conn_id == _host_conn_id`) | Sound *given* (a) |
| (f) | Seat index from (d) | Sound given (d) |

**The chain never closes between (b)/(c) and (e).** Authorization rests
entirely on (a). The signing key authenticates each message and then
decides nothing.

---

## 3. Hostile cases

| Attack | Outcome | Why |
|---|---|---|
| Arbitrary TCP client connects | **Succeeds.** Enters the roster on `player_info` | No credential is required. This is M-8 |
| Claims another peer's pubkey | **Fails.** `wire.unpack` rejects the envelope — the attacker lacks the private key | |
| Claims another peer's `conn_id` | **Fails.** `conn_id` is assigned server-side per socket and never read from the wire | |
| Reconnects under a new `conn_id` | **Succeeds**, as a *new* player. The old seat is orphaned, not inherited | Seats are keyed by `conn_id`, so a reconnect is a different player |
| Signs with a key never admitted | **Accepted.** Nothing compares a message's `pubkey` to the one recorded at `player_info` | The (b)→(e) gap. No authorization impact today because authorization is by `conn_id` |
| Races legitimate admission to become host | **Blocked in practice.** `_on_player_ack` is LOBBY-only and accepts an unknown host once — but only the host calls `start_host()`; joiners only `connect()`, so a joiner's sole peer is the host it dialled. Closed by topology, not by the check |
| Joins after capability freeze | **Blocked.** `_on_game_start` refuses changes once `PLAYING`/terminal | |
| Obtains multiple seats | **Succeeds.** N connections → N `conn_id`s → N roster entries. Classic Sybil | |

---

## 4. Finding A — the signing identity is not load-bearing

`wire.unpack` verifies every envelope (C-1). `Session` then authorizes
exclusively on `conn_id`. `msg["pubkey"]` is read in exactly one place,
`_on_player_info`, and only to populate a display field:

```python
peer_id = msg.get("pubkey", "")[:16],
```

And the verification is **self-certifying**. `wire.unpack` checks the
signature against the pubkey carried *in the same envelope*:

```python
if not identity.verify(pubkey, canonical, sig):
```

There is no allowlist, no roster comparison, no check that the key was
ever admitted — confirmed by inspection of `unpack`. Any freshly
generated keypair produces a valid envelope. So the signature layer
provides **integrity and sender-consistency-within-one-message**, not
authentication of a known party. Calling it "signature-verified" is
true and, on its own, means less than it sounds.

Consequences:

* A connection may present key K₁ at `player_info` and sign every
  subsequent message with K₂. Nothing notices.
* Signatures provide no non-repudiation in practice: no stored decision
  references the key that authorized it.
* The security of every host-gated and seat-gated check reduces to *"the
  right TCP socket"*.

Not exploitable today — `conn_id` is unforgeable, so socket identity is
a real (if weak) identity. But it means the cryptographic identity layer
is decorative, and any future change that keys authorization on
`peer_id` would inherit an unpinned binding.

## 5. Finding B — `_on_player_list` accepts roster writes from any peer

`_on_game_start` and `_on_player_ack` both gate on
`conn_id != self._host_conn_id`. `_on_player_list` does not:

```python
def _on_player_list(self, conn_id: str, msg: dict) -> None:
    """Non-host receives updated player list from the host."""
    for p in payload.get("players", []):
        ...  # creates or mutates self.players[cid]
```

Any connected peer can therefore inject roster entries and overwrite
`nickname`, `avatar_b64`, `is_host`, `ready`, `x25519_pubkey_hex`, and
rewrite `_join_order` on any non-host peer.

**Impact today is limited**, and the audit should say so rather than
inflate it: seat order comes from host-gated `game_start`, not from this
list; `_host_conn_id` comes from `player_ack`; and
`x25519_pubkey_hex` — despite being declared for hole-card
encryption — is currently used **nowhere outside `session.py`**. The
hostless deal distributes threshold `deal_share` messages instead.

It is nonetheless a real inconsistency with its two siblings, and it is
pre-positioned to become severe the moment `x25519_pubkey_hex` is
actually used to encrypt anything, because the attacker chooses the key
material a victim would encrypt to.

## 6. Finding C — the invite already carries an unused credential

`invite.py` mints a room code as
`peer_id_prefix[8] || rendezvous_key[8]`, and its own docstring says the
prefix "lets joiners verify they connected to the right host before the
DKG handshake begins (M-7)".

Neither half is checked. `rendezvous_key` is used only as a LAN
multicast discovery tag in `transport.announce`/`find_peer`;
`peer_id_prefix` is never compared to the host's actual key. The
credential needed to close M-8 already exists, is already distributed
out-of-band with the invite, and is simply never presented or verified.

## 7. Finding D — topology is a star, the deal assumes a mesh

Only the host calls `start_host()`; joiners only `connect()`. The host
re-broadcasts **chat** and nothing else — deal messages are not relayed.
So at three or more peers, two joiners have no path to each other, while
`_flush_deal` broadcasts on the assumption every peer sees it.

At two peers, star and mesh coincide, which is why this has not
surfaced. Recorded here because the trust model differs sharply between
"one connection, to the host" and "a connection to everyone", and the
admission design should not assume the second before it exists.

---

## 8. What property the protocol actually requires

Not accounts, not a PKI, not OAuth. Three properties, in dependency
order:

1. **Continuity.** Every message attributed to a seat comes from the
   same keyholder for the life of the table. This is what makes the
   signature layer mean anything, and it is a pure local invariant: pin
   `pubkey` to `conn_id` on first sight, reject any change.
2. **Admission.** Only a party that holds the out-of-band invite may
   enter the roster. Proving knowledge of `rendezvous_key` — challenge
   from the host, HMAC or signature over it plus the `conn_id` — is
   sufficient, needs no new infrastructure, and is the credential the
   room code already carries.
3. **Host authenticity.** The joiner verifies the host's Ed25519 pubkey
   against `peer_id_prefix` from the room code before sending anything
   (M-7), so a joiner cannot be lured onto an impostor host that would
   then choose seat order and prevention mode.

Sybil resistance is **not** achievable at this layer. N invite-holders
can always open N connections. That is an invite-distribution policy
question (single-use codes, host-side seat caps), not a cryptographic
one, and pretending otherwise would be the expensive kind of mistake.

**Migration:** (1) is backward-compatible — pure tightening, no wire
change. (3) is backward-compatible — the joiner checks data it already
has. (2) is a wire change requiring a challenge/response before roster
entry, so it needs the capability/version negotiation that does not yet
exist, and it should land last.

---

## 9. Smallest next implementation goal

> Pin the Ed25519 pubkey to the `conn_id` on the first signed message
> from that connection, and reject any later message from the same
> `conn_id` bearing a different pubkey — attributed, fail-closed, with
> the same abort semantics as a bad proof. Add the `_on_player_list`
> host gate in the same change, matching `_on_game_start`.

Rationale: it closes Finding A and Finding B, changes no wire format,
requires no negotiation, and is the invariant properties 2 and 3 both
assume. Neither Bayer–Groth semantics nor the DKG is touched.

Deliberate breaks to control it: (i) accept a second pubkey on a pinned
`conn_id` — must fire the new pinning test alone; (ii) remove the
`player_list` host gate — must fire the roster test alone.
