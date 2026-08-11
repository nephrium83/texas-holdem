# Star vs mesh — the finding, and the choice it forces

**The finding is confirmed.** Not by reading: by three real processes on
the production transport.

No implementation follows. This document ends at a recommendation.

---

## 1. What was measured

`tests/test_three_peer_topology.py` starts three `tests/prod_peer.py`
subprocesses over `holdem.p2p.transport` — the module `onboarding.py`
actually uses. One host, two joiners, wired exactly as onboarding wires
them: only the host calls `start_host()`, joiners only `connect()` to
the host's address.

`holdem.p2p.transport` keeps `_loop`, `_writers` and `_server` at module
scope, so one peer per process. Three peers therefore means three
processes, which is why a subprocess harness exists rather than a
fixture.

Measured connection graph:

| Peer | connections |
|---|---|
| host A | **2** |
| joiner B | **1** (to A) |
| joiner C | **1** (to A) |
| B ↔ C | **absent** |

Then seat B broadcast a `key_announce` — the first message every seat
sends, and one every other seat needs to derive the joint key:

```
required recipients : {A, C}
actual recipients   : {A}
first missing message: key_announce, seat 1 -> seat 2
```

The host received it. **C never did**, and nothing relays it: `_on_chat`
is the only `is_host` re-broadcast in `session.py`. The message is
silently lost — no error, no log, no protocol outcome.

**A three-seat hand cannot leave KEYGEN.** At two peers a star and a
mesh are the same graph, which is why every existing test passes.

`test_joiner_broadcast_does_not_reach_the_other_joiner` is marked
`xfail(strict=True)`: it records the defect without a red CI, and it
fails loudly the moment the topology is fixed and the xfail becomes a
lie.

---

## 2. Which of the three the session layer does

Of the three possibilities the goal listed — mesh, relay, or silent loss
— the answer is **silent loss**. Not a degraded mesh, not an unreliable
relay. The sender's `broadcast()` iterates its own `_writers`, finds
only the host, and returns success.

---

## 3. The two candidate architectures

### Option 1 — true mesh

The host introduces peers; each joiner opens direct authenticated
connections to every other seat.

| Dimension | Assessment |
|---|---|
| Connections | n(n−1)/2. At 9 seats, 36 — fine |
| NAT traversal | **The hard part.** Today only the host must be reachable, and STUN + relay fallback already exist for exactly one endpoint. A mesh needs *every* peer reachable by every other. Two joiners behind symmetric NATs cannot connect without TURN-style relaying — so the relay comes back anyway, just less predictably |
| Identity | Best fit. Each edge is a real connection, so `conn_id` keeps its meaning and the M-8 continuity work applies unchanged. Every message is received directly from its author |
| Ordering | Per-edge TCP ordering only. No global order, but the protocol already tolerates that (`_hand_msg_ok` buffers, the shuffle chain is explicitly sequenced) |
| Duplicates | Naturally none — one path per pair |
| Host failure | Only the introduction is lost. Existing edges survive, which is what makes "hostless" honest |
| "Hostless" | Genuinely hostless after introduction |

### Option 2 — authenticated host relay

Keep the star. The host forwards every hostless protocol message,
without gaining authority over it.

| Dimension | Assessment |
|---|---|
| Connections | n−1. Trivial |
| NAT traversal | **Already solved.** One reachable endpoint, which STUN and the relay fallback are already built for. This is the decisive practical advantage |
| Identity | Requires the M-8 continuity work as a *precondition*, not a follow-up. A relayed message arrives on the host's `conn_id`, so authorization by `conn_id` — which is the whole current model — breaks. Authorization must move to the **signed author**, and the signature must be verified against a pinned per-seat key. Note this is exactly the "signing identity is decorative" finding, promoted from theoretical to load-bearing |
| Ordering | The host becomes a total-order point. Convenient, and a single point of reordering |
| Duplicates | Must be handled: the host echoes to the sender today (`_on_chat`), and replay needs sequence numbers |
| Host failure | Fatal to the hand. Every hostless message stops |
| "Hostless" | Weakened, honestly stated: the host cannot **forge** (signatures) or **alter** (signatures) messages, but it can **suppress** or **delay** them, and detecting suppression needs per-seat sequence numbers plus a timeout. "Hostless" becomes "the host cannot cheat undetectably", not "the host has no power" |

---

## 4. Recommendation: Option 2, authenticated host relay

Three reasons, in order of weight.

**NAT.** This is a LAN-and-internet peer application with STUN and a
relay fallback already built for a single reachable endpoint. A mesh
requires every pair to traverse, and the honest version of Option 1
ends in TURN-style relaying for the pairs that fail — a relay, with
worse failure modes and no single place to reason about them. Choosing
relay explicitly is choosing the thing we would arrive at anyway.

**It forces the identity work to be correct rather than optional.**
Under relay, authorization *must* move from `conn_id` to the signed
author with a pinned key. The M-8 audit called the signature layer
decorative; relay makes it load-bearing. That is the right direction
regardless of topology, and relay is what makes it non-negotiable.

**Host failure is already fatal.** `_on_game_start` is host-only and
frozen; `_on_player_ack` establishes host identity; onboarding has one
listener. A hand does not currently survive host loss, so relay does not
give that away — active-hand host migration was deliberately removed
(`9ee3804`).

The cost is stated plainly: **a relaying host can suppress or delay
messages.** That must be detectable, not merely disallowed by intent.
Per-seat monotonic sequence numbers plus the existing timeout machinery
make suppression a visible protocol outcome rather than a hang — which
is the same standard the Bayer–Groth work was held to.

---

## 5. Precondition, then the implementation goal

The relay design **depends on** the M-8 continuity work, so that lands
first — not as a follow-up. But relay also changes what that work must
be, and the change is not cosmetic.

**`pubkey ↔ conn_id` pinning is the wrong shape under relay.** The M-8
audit proposed it while assuming one connection carries one author. A
relaying host breaks exactly that assumption: the single host connection
a joiner holds legitimately carries messages authored by *every* other
seat. Pinning one key to that connection would reject all but the first
author, and loosening the pin to "any admitted key" would restore the
decorative signature the audit complained about.

The model must separate two things the code currently conflates:

| Concept | Is | Scope |
|---|---|---|
| **Transport hop** | `conn_id` | who handed me these bytes |
| **Protocol author** | authenticated signing key → seat | who said it |

`conn_id` remains sound for what it actually attests — the hop — and
keeps its role in host-gated checks like `game_start`. It must stop
being the identity that authorizes *seat* actions.

So the precondition becomes:

> Bind **seat → signing key** at hand start, and authorize every
> seat-scoped message on the authenticated author of its envelope rather
> than on the connection it arrived over. Add the `_on_player_list` host
> gate in the same change.

`_on_player_list`'s gate is unaffected by this correction: that message
is host-authored by definition, so it is a hop-level check and stays
one.

Then:

> Relay hostless protocol messages through the host. The host forwards
> `key_announce`, `deck_round`, `deal_share`, `audit_open`, `bet_action`
> and `hand_void` to every peer except the author, **preserving the
> original signed envelope byte-for-byte** — it is a courier, never a
> re-signer, so a relayed message is indistinguishable from a direct one
> to the recipient's verifier. Recipients authenticate the **author's
> signature**, not the delivering connection, and reject any message
> whose author key does not match the key bound to the seat it claims. Each seat numbers its own messages monotonically
> per hand; a gap is a protocol outcome, not a stall. Gate on
> `test_joiner_broadcast_does_not_reach_the_other_joiner` flipping from
> xfail to pass — `strict=True` means it fails if it passes early.

Not in scope for that goal: mesh connections, host migration, and any
Bayer–Groth change. The proof layer is indifferent to how bytes arrive.
