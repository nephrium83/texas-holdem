# Multiplayer design

Status: **design**. No networking code exists yet. This document is the
plan the current codebase is being shaped toward. It records decisions
already made so they survive between work sessions.

The headline choice: **serverless, peer-to-peer, play money.** Strangers
should be able to play a fair game with no central server and no trusted
dealer. That is a solved problem in cryptography — *mental poker*, posed
by Shamir, Rivest, and Adleman in 1979 — and this is a plan to implement
a practical, modern version of it.

Play money only. Real-money handling turns a cryptography project into a
regulated casino and invites an entirely different threat model; it is
out of scope permanently.

---

## Why serverless

Two independent problems, deliberately separated:

1. **Trust without an authority** — can mutually distrusting players
   agree on a fair shuffle, keep hole cards private, and settle a pot,
   with nobody able to cheat undetectably? This is the cryptography.
2. **Reaching peers without infrastructure** — how do 2–9 strangers
   find each other and exchange messages with no server we run? This is
   the transport.

They are solved by different tools and can be built in either order.

The engine already earns its keep here: it is a **deterministic state
machine**. The same ordered log of actions produces the same state on
every machine — the test suite asserts exactly this (chip conservation,
legality, and matched bets on every replay across thousands of fuzzed
hands). So "shared game state" needs no server: every client replays the
same signed action log locally, and the engine itself defines whose turn
it is, which makes action ordering nearly free and makes any illegal or
out-of-turn action attributable to the signature that sent it.

---

## Trust model (mental poker)

**Shuffle.** Each player encrypts and shuffles the deck in sequence
under a commutative cipher, so the final deck is locked under *every*
player's key in an order nobody knows. This descends from the
Barnett–Smart (2003) protocols: ElGamal plus a zero-knowledge
verifiable-shuffle argument, so a player who shuffles dishonestly
produces a mathematically attributable cheat proof. Randomness is
contributed by all players jointly, which also retires the "who do you
trust to shuffle" question entirely.

**Dealing and showdown.** Dealing a hole card to a player means every
*other* player strips their encryption layer, leaving a card only the
recipient can finally decrypt. Showdown reveals work the same way.
Mucking is simply declining to decrypt: you forfeit any claim on the pot
but keep your information private.

**The guarantee** is strictly stronger than any server, including one we
run: your hole cards stay private unless *every other player at the
table colludes*.

**What cryptography cannot fix**, stated plainly:
- Players sharing their own hole cards off-band (screenshots over
  Discord). Real sites can only catch this statistically.
- Sybil seats: one stranger can be several seats. Tolerable at play
  money; fatal with real money.
- A modified client, or a solver on a second monitor. Same category as
  the above — unenforceable at the protocol layer.

---

## Threshold keys and the dropout problem

Naive n-of-n encryption makes "a player dropped" unrecoverable: a
vanished player's key layers can never be stripped, so the hand cannot
proceed — and if a stalled hand refunds committed chips, that is an
**undo button** (drop when dealt a bad board, or when about to lose a
big pot, and get your money back). That is a game-breaking exploit, not
an inconvenience.

**Fix: per-hand threshold key generation.** At the start of each hand
players run a distributed key generation so the joint decryption key is
secret-shared *t*-of-*n*. Any *t* players can then reconstruct a
dropped player's contribution: their pending action becomes a fold,
their committed chips stay in the pot, and the hand finishes. Dropping
becomes *exactly* folding — which is always legal — so there is nothing
to gain by pulling the cable.

**The unavoidable tradeoff: the liveness threshold is the collusion
threshold.** Any *t* players who can rescue a hand from a dropout can
also, off-protocol, pool their shares to read every hole card. There is
no way around this; it is a dial, not a free choice. For 2–9 strangers
at play money, **t ≈ ⌈2n/3⌉** is the chosen point: enough liveness to
survive a realistic number of simultaneous drops, while requiring a
large coordinated majority to break privacy — and a majority that
motivated has easier ways to cheat at a play-money table.

---

## Dropout and timeout rules

These are protocol rules, not UI. The single-player action clock is the
prototype for the timeout half.

- **Actions pending → timeout folds** (checks if checking is free). The
  table attests "seat X timed out at sequence N" with *t* signatures.
  Nobody can sign an action *for* you, but the table can attest your
  *silence* — and silence can only ever fold, never commit chips.
- **All-in with no action left → the hand stays live.** Reconstruction
  opens the cards at showdown and the absent player can still win. This
  is the standard "all-in on disconnect" protection, and it prevents the
  mirror-image attack: **DoS the chip leader at showdown to muck his
  winning hand.** Over-punishing dropouts weaponizes disconnection, so
  the rules must distinguish these cases.
- **Reconnect grace** before the clock fires (signed session resume),
  because home-wifi hiccups are the common case, not cheating.
- **Unanimous signed abort** is the *only* legitimate refund path — every
  contested player signs it. No victim, no exploit.

Residual griefing (drop-and-rejoin scouting, serial timeout stalling)
is handled with cardroom conventions — missed-blind rules, a per-pubkey
strike list, table admission control — not cryptography.

---

## Action log

Every action is signed by the acting seat's key and carries the hash of
the previous log entry: a **hash-chained, signed, append-only log.**

This is a blockchain only in the sense `git` is one — the tamper-evident
data structure, deliberately **without** the consensus machinery. There
is no proof-of-work, no token, no global agreement protocol. Bitcoin's
innovation was consensus on ordering among an open, Sybil-infested,
anonymous set; this table has a small set of known pubkeys and derives
turn order from the game rules, so none of that applies. Anyone
proposing to put poker "on chain" is importing the wrong half.

The whole hand transcript is therefore exportable as a self-verifying
artifact, and any divergence between clients is pinned to a signature.

---

## Transport (rendezvous)

Trust and transport are independent; transport can be swapped without
touching the protocol. The chosen primary is **libp2p**: Kademlia DHT
for peer and table discovery, QUIC/TCP for data, circuit relay v2 for
NAT traversal — all without a server we operate. Options, from "nothing
but us" outward:

- **Direct / LAN** — hand a join code to friends on the same network.
  Zero third parties.
- **libp2p (primary)** — Kademlia DHT maps a join code to peer
  multiaddrs; circuit relays contributed by other app users handle
  symmetric NAT without port-forwarding or a TURN server fleet. A user
  downloads the app and starts playing — no IP address to enter, no
  server to run.

> **Scope decision (2026-07-18): broadband only.** This is a broadband
> app. Mesh-radio / LoRa-class transports (Reticulum) are out of scope:
> the anti-cheat protocol assumes broadband-class bandwidth (per-hand
> proof traffic in the megabytes is acceptable), and no design decision
> should be constrained by narrowband links. An earlier draft slotted
> Reticulum behind the Phase 1 stream interface for mesh/offline
> scenarios; that option is retired.

---

## Settings and the table contract

Implemented now, in [`holdem/settings.py`](../holdem/settings.py). Every
option has exactly one **scope**, and the scopes are what make the game
multiplayer-ready:

- **CLIENT** — this machine only: theme, animation speed, local
  conveniences, and single-player-only aids. Persisted to a per-user
  config file (which also fixes settings resetting each launch). One
  notable CLIENT key: **`fullscreen`** (bool, default `true`) — the
  window launches maximised; the value persists across sessions and is
  toggled at any time with F11.
- **TABLE_RULE** — the contract every seat plays under: stakes,
  structure, timing (clock and time-bank parameters live here — the
  clock is the liveness rule, not a preference), and which extras are
  allowed. `settings.rules_hash()` reduces the rule set to a short
  canonical id. **A multiplayer join code embeds this hash**, so every
  client can verify it is playing the same game; changing a rule
  mid-session requires unanimous signed consent (the same machinery as
  the abort).
- **SEAT** — per-seat lifecycle actions (sit out, straddle arm, top-up).
  Not settings at all: they are protocol messages, rendered as buttons.

Two consequences already reflected in the code:

- **Training aids are RTA.** Live equity and coaching hints are a
  built-in solver; observe mode is botting. They are gated by a
  `training_aids` table rule and hidden at human tables unless the table
  agrees to allow them. Enforcement beyond the official client is
  social — the same unenforceable boundary as off-band collusion.
- **Some settings dissolve into protocol.** "Show mucked cards" is a
  local display toggle today; under mental poker a mucked card is simply
  never decrypted, so it cannot be mis-set. Rabbit hunting becomes a
  consent action that costs the table a cooperative decryption round.

---

## Roadmap

Ordered so each phase is independently testable, in the house style
(deterministic replay, transcript verification, adversarial fuzzing).

**Phase 0 — settings scaffolding.** *Done.* Scope tags, the rules hash,
and config persistence are in place, so table rules are already a
first-class, hashable object.

**Phase 1 — protocol spec.** Wire format for signed actions, the
hash-chained log, the per-hand DKG handshake, and the dropout/timeout
state machine. Transport-agnostic. Deliverable: a written spec plus a
reference codec with round-trip tests. This is the part that makes the
project rare and should come first.

**Phase 2 — joint shuffle prototype.** Commutative encryption, the
sequential shuffle, threshold dealing, and verifiable-shuffle proofs —
run locally across simulated players, no network. Property-tested to
death: replay determinism, an oracle for the shuffle proofs, and a
no-leak assertion (no serialized payload for seat N ever contains
another seat's cards). Expect this to be the single hardest piece;
library support is thin and much of it is built from the papers.

**Phase 3 — transport.** libp2p is the primary target: a user downloads
the app and starts playing with no IP to enter and no server to run.
Peer discovery uses the Kademlia DHT; NAT traversal uses circuit relays
contributed by peers already in the swarm, with DCUtR hole-punching
attempted first. A table is a join code that encodes the rules hash; the
DHT maps that code to the multiaddrs of the hosting peer. Signed action
envelopes from Phase 1 flow over libp2p streams — the transport sees
opaque signed bytes and is untrusted by design. Implementation is Python
(py-libp2p) with a Go libp2p sidecar subprocess as the fallback if
py-libp2p's circuit relay or DHT coverage proves insufficient. An
adversarial-dropper bot — yanking simulated players at every protocol
step and asserting the hand always terminates with conserved chips —
doubles as the integration fuzzer.

**Phase 4 — multiplayer client and packaging.** Phase 4 wires libp2p
(py-libp2p, falling back to the Go sidecar per the Phase 3 decision
rule) into the client, makes the lobby do real DHT-based table discovery
and joining, and runs actual multiplayer game sessions over the Phase 1
signed wire format with Phase 2 mental poker shuffles.

**Client direction (decided).** The shipped client is a **native Godot
2.5D app** — a retro pixel-art table with a fixed camera and occasional
detailed (dimensional) animations on key moments (showdown flip, pot
award, deal). The **Python engine runs as an authoritative sidecar**:
Godot handles rendering and input only and never runs game logic, so
the security-critical core (evaluator, betting state machine, signed
envelopes, verifiable shuffle) stays a single source of truth. The
Tkinter GUI is **not** the shipped client — it is retained as a
development and headless-test harness for the engine. **This is a
native desktop app; a web/browser client is explicitly out of scope,
permanently.** The client ↔ engine message contract (what the client
sends as actions and receives as state snapshots + the animation-
triggering event stream) is a slice of the Phase 1 spec and is shared
across any client, so it is pinned before the Godot client is built.

The deliverable is a packaged native binary — Godot export for the
front end plus the bundled Python sidecar — one download, no separate
Python install required.

The current single-player engine is not throwaway: it is the
authoritative rules core the protocol wraps, and its determinism is the
property the whole design leans on. The Godot spike (a fixed-perspective
pixel table dealt from the sidecar, with one card flipping through 3D
space on showdown) validates both the aesthetic and the sidecar
architecture, and can proceed in parallel once the client ↔ engine
contract is pinned.


---

## Phase 1 — Protocol spec

The four components below are the Phase 1 deliverable. Each is
transport-agnostic and independently testable. The canonical test harness
runs multiple simulated peers in-process, feeding signed byte strings
directly to codec functions and asserting round-trip fidelity, chain
validity, and FSM state invariants with no network involved.

---

### 1. Signed action wire format

Every game event — player action or dealing step — travels as a **signed
envelope**. The envelope is a JSON object with the following required fields.
No optional fields exist; receivers reject any envelope with unknown or
missing keys.

```json
{
  "v":       1,
  "action":  "<action_type>",
  "hand_id": "<uuid-v4>",
  "pubkey":  "<hex Ed25519 public key, 32 bytes / 64 hex chars>",
  "seq":     0,
  "ts":      0,
  "payload": {},
  "sig":     "<hex Ed25519 signature, 64 bytes / 128 hex chars>"
}
```

- **`v`** — envelope version; this spec defines version `1`.
- **`action`** — one of the action types listed below.
- **`hand_id`** — opaque identifier for the hand; derived in §3.
- **`pubkey`** — the sender's long-term Ed25519 public key, hex-encoded.
- **`seq`** — a per-seat unsigned 64-bit integer, strictly increasing with
  every envelope a seat sends. Receivers reject any `seq` not greater than
  the last seen `seq` from that `pubkey`.
- **`ts`** — Unix time in milliseconds (UTC). Receivers reject envelopes
  whose `ts` is more than 60 000 ms from their own wall clock.
- **`payload`** — action-specific data (defined per action type below).
- **`sig`** — Ed25519 signature over the canonical pre-image (see below).

**Action types and their payloads:**

- `"bet"` — place a wager. Payload: `{"amount": <chips>}`.
- `"call"` — match the current bet. Payload: `{}`.
- `"check"` — decline to bet when no bet is owed. Payload: `{}`.
- `"fold"` — discard the hand. Payload: `{}`.
- `"raise"` — increase the current bet. Payload: `{"amount": <total chips
  committed this street>}`.
- `"allin"` — commit all remaining chips. Payload: `{"amount": <total chips
  committed>}`.
- `"dkg_commit"` — DKG phase-1 commitment (§3). Payload: `{"commit": "<Ci>"}`.
- `"dkg_reveal"` — DKG phase-2 reveal (§3). Payload: `{"reveal": "<Ri>",
  "shares": {"<seat_j>": "<Sij>", ...}}`.
- `"dkg_verify"` — DKG phase-3 acknowledgment. Payload: `{"ok": true}` or
  `{"ok": false, "reason": "bad_share:<seat_i>"}`.
- `"deal_step"` — one seat's contribution to the cooperative shuffle or
  deal; payload structure defined in Phase 2.
- `"hand_start"` — genesis action; sent once by the table initiator at hand
  open (payload defined in §2).
- `"timeout_attest"` — co-signed attestation that a seat has timed out.
  Payload: `{"seat": <int>, "seq_expected": <uint64>, "attestations":
  ["<sig>", ...]}`. Requires `t` valid attestations from distinct active
  seats before the fold is recorded.
- `"abort"` — unanimous signed table abort; requires every active seat to
  have contributed a signature in `attestations`. Payload: `{"reason":
  "<string>", "attestations": ["<sig>", ...]}`.
- `"player_info"` — identity advertisement broadcast by a peer immediately
  after the DKG handshake completes. Payload:
  ```json
  {
    "nickname":   "<display name, max 20 chars>",
    "avatar_b64": "<base64-encoded 64×64 PNG, max ~10 KB>",
    "pubkey":     "<Ed25519 hex (redundant with envelope field, explicit here for self-description)>"
  }
  ```
  When a peer joins a table, it broadcasts a `player_info` action
  immediately after receiving a valid `dkg_verify` acknowledgement from
  every other seat. All peers store the received avatar bytes keyed by the
  sender's `pubkey` and display them at the corresponding seat. The
  `pubkey` field in the payload must equal the `pubkey` field in the
  enclosing envelope; receivers drop any `player_info` where they differ.
  `avatar_b64` is the output of `onboarding.compute_avatar_b64()` — a
  64×64 PNG thumbnail rendered and base64-encoded during onboarding.
  Receivers that cannot decode the field (bad base64, non-PNG, oversized)
  silently fall back to the colored-circle placeholder used for AI seats.

**Canonical serialization for signing.** The pre-image is the envelope
minus the `"sig"` key, serialized with sorted keys, compact separators
(`","` and `":"`), no whitespace, UTF-8 encoded:

```python
import json

def canonical(envelope: dict) -> bytes:
    body = {k: v for k, v in envelope.items() if k != "sig"}
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
```

The signature is `Ed25519.sign(private_key, canonical(envelope))`. Receivers
call `Ed25519.verify(envelope["pubkey"], canonical(envelope), envelope["sig"])`
and drop any envelope that fails.

---

### 2. Hash-chained action log

Every signature-valid envelope is wrapped in a **chain entry** before being
appended to the local log. Chain entries link each action irrevocably to its
predecessor, making any tampering attributable to a specific signature.

**Chain entry structure:**

```json
{
  "prev":     "<SHA-256 hex of canonical bytes of previous chain entry>",
  "envelope": {}
}
```

The canonical bytes of a chain entry are computed identically to the envelope
pre-image: `json.dumps(entry, sort_keys=True, separators=(",", ":"),
ensure_ascii=False).encode("utf-8")`.

**Genesis entry.** The first entry of every hand has a synthetic predecessor
that encodes the hand identity:

```json
{
  "prev": "<hand_id>:genesis",
  "envelope": {
    "v":       1,
    "action":  "hand_start",
    "hand_id": "<hand_id>",
    "pubkey":  "<initiator pubkey>",
    "seq":     0,
    "ts":      1234567890000,
    "payload": {
      "rules_hash": "<10-hex from settings.rules_hash()>",
      "seats": [
        {"seat": 0, "pubkey": "<hex>", "stack": 1000},
        {"seat": 1, "pubkey": "<hex>", "stack": 1000}
      ]
    },
    "sig": "<sig over canonical of above minus sig>"
  }
}
```

The `hand_id` is derived deterministically:
`SHA-256(rules_hash + "|" + seat_pubkeys_sorted_joined_by_"|" + "|" +
timestamp_ms_as_string)`, hex-encoded, first 32 characters. This is
collision-resistant across all hands a session will ever produce, and any
peer can verify it from the `hand_start` payload alone.

**Verification algorithm.** Peers run this on receipt of any entry batch,
on reconnect, or to audit the full hand log:

```python
import hashlib, json

def verify_chain(entries: list[dict], hand_id: str) -> bool:
    expected_prev = f"{hand_id}:genesis"
    for entry in entries:
        # 1. Linkage check
        if entry["prev"] != expected_prev:
            return False
        # 2. Signature check
        env = entry["envelope"]
        if not ed25519_verify(env["pubkey"], canonical(env), env["sig"]):
            return False
        # 3. Advance the cursor
        raw = json.dumps(
            entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        expected_prev = hashlib.sha256(raw).hexdigest()
    return True
```

A peer that receives a chain entry breaking the linkage or carrying an
invalid signature broadcasts a signed accusation (action `"chain_fault"`,
payload: the offending entry) and stops accepting actions from that `pubkey`
for the remainder of the hand. **Forks** — two entries with the same `prev`
— are resolved by the rule that the entry with the lower `seq` from the
acting seat wins; the other is dropped and its sender is flagged as a griever.

The full hand transcript — genesis entry through final showdown — is a
self-verifying artifact. Any third party with the participants' public keys
can replay and audit a hand with no trust in the node that exported it.

---

### 3. Per-hand threshold DKG handshake

At the start of each hand, before any cards are dealt, active seats run a
**distributed key generation** ceremony. The output is a joint public key
`PK` (the shared deck encryption key) and a per-seat secret share `si`.
The threshold is `t = ceil(2n/3)` for `n` active seats: any `t` seats can
reconstruct a missing seat's contribution; no `t-1` seats can learn
anything about another seat's share. The underlying construction (Pedersen
DKG over the curve used for the commutative shuffle cipher) is defined in
Phase 2; this section specifies only the handshake message sequence.

All DKG messages are signed envelopes recorded in the action log.

**Handshake sequence:**

```
PHASE 1 — COMMIT  (timeout: 10 s from hand_start)
  Each seat i broadcasts dkg_commit:
    payload.commit = Ci     commitment to seat i's random polynomial
  Completion: all n expected seats have sent dkg_commit.

PHASE 2 — REVEAL  (timeout: 10 s from last required dkg_commit)
  Each seat i broadcasts dkg_reveal:
    payload.reveal = Ri     opening of Ci
    payload.shares = { j: Sij  for each other seat j }
                            Sij is the share for seat j, encrypted
                            under seat j's long-term public key
  Completion: all n expected seats have sent dkg_reveal.

PHASE 3 — VERIFY  (timeout: 10 s from last required dkg_reveal)
  Each seat j decrypts its n-1 incoming shares, checks each against the
  matching public commitment Ci, and broadcasts dkg_verify:
    payload.ok = true
    — or —
    payload.ok = false, payload.reason = "bad_share:<seat_i>"
  Completion: all n expected seats have sent dkg_verify with ok = true.

PHASE 4 — KEY ASSEMBLY  (no message required)
  Each seat independently assembles PK from the public commitments already
  in the log. The result is identical on every peer. hand_start plus all
  DKG envelopes are the sole inputs; no further agreement message is needed.
```

**Dropout handling during DKG.** A seat that fails to produce a required
DKG message before its step timeout is handled as follows.

- **Missing `dkg_commit` (Phase 1 timeout):** the remaining active seats
  co-sign a `timeout_attest` naming the absent seat. That seat transitions
  to KICKED (§4). The DKG restarts from Phase 1 with `n' = n − 1`. If
  `n' < 2`, the hand is void and all stacks are restored — an automatic
  protocol refund, not a player-triggered abort.
- **Missing `dkg_reveal` (Phase 2 timeout):** same co-sign and KICKED
  procedure. The absent seat's commit is discarded. DKG restarts with
  `n' = n − 1`.
- **Missing `dkg_verify` (Phase 3 timeout):** the remaining seats check
  whether the absent seat's shares (already broadcast in Phase 2) were
  consistent with its commit. If consistent, the seat transitions to
  TIMED_OUT rather than KICKED — a transient failure at the final step is
  most likely a network hiccup, not a cheat — and its public contribution
  is extracted from its commit to assemble PK without that seat's further
  participation. If the Phase 2 shares were inconsistent with the commit,
  the seat is KICKED and the DKG restarts.
- **Re-run limit.** DKG may restart at most `n − 2` times per hand (which
  preserves the minimum two-seat game). A seat that triggers two restarts
  within a single hand is KICKED and barred from future hands in the session
  under that pubkey.

---

### 4. Dropout and timeout state machine

Each seat in an active hand is in exactly one of the following states.

**ACTIVE** — the seat is connected and participating normally.

**TIMED_OUT** — the action clock expired before the seat acted. The seat may
still be reachable; a 15 s grace period allows it to send a valid action
before the auto-fold fires. No chips are forfeited yet.

**DISCONNECTED** — no signed message has been received from the seat for
more than 60 s. Treated identically to TIMED_OUT for action-clock purposes;
the seat remains eligible to reconnect.

**FOLDED_AUTO** — the seat was moved to fold by an expired timeout or
reconnection window, attested by `t` co-signers. The seat has no further
decisions this hand; its in-street commitments stay in the pot, consistent
with a voluntary fold at the same moment.

**ALL_IN_ABSENT** — the seat committed all its chips (all-in) and then
disconnected. Because it has no future action decisions, it stays in the
hand and is eligible to win at showdown. The threshold reconstruction
mechanism handles its hole-card decryption without its participation.

**KICKED** — the seat was removed from the hand, ordinarily because it
triggered a DKG failure or exceeded the re-run limit. Chips committed before
the kick remain in the pot up to any side-pot boundary; the remaining stack
is held in escrow until hand end, then returned.

**Transitions:**

```
ACTIVE
  → TIMED_OUT      action clock expires (clock_base seconds; TABLE_RULE default 25 s)
  → DISCONNECTED   no message received within 60 s
  → ALL_IN_ABSENT  seat commits all chips and then disconnects

TIMED_OUT
  → ACTIVE         seat sends a valid action within the 15 s grace period
  → FOLDED_AUTO    grace period expires; t co-signers attest the silence;
                   synthetic fold envelope recorded in the log

DISCONNECTED
  → ACTIVE         seat sends a signed session-resume within 60 s
  → FOLDED_AUTO    reconnection window expires; attested and auto-folded
  → ALL_IN_ABSENT  if the seat was already all-in at disconnect time

FOLDED_AUTO       (terminal for this hand)

ALL_IN_ABSENT
  → ACTIVE         seat reconnects before showdown (may observe; no decisions remain)
  → (terminal)     if no reconnect before showdown; threshold reconstruction proceeds

KICKED            (terminal for this hand)
```

**Timeout thresholds** (all TABLE_RULE; defaults align with the existing
single-player clock in `settings.py`):

| Threshold | Default | Description |
|---|---|---|
| Action clock (`clock_base`) | 25 s | Per-action budget, extendable by time-bank draws |
| Grace period | 15 s | Extra window after TIMED_OUT before auto-fold fires |
| Reconnection window | 60 s | Window to resume a session after DISCONNECTED |
| DKG step timeout | 10 s | Per-phase budget during the DKG handshake (§3) |

**Attesting a timeout.** No single peer can fold another seat unilaterally.
A timeout fold requires `t` distinct active seats to broadcast
`timeout_attest` envelopes carrying matching `(seat, seq_expected)` tuples.
When `t` valid, distinct attestations for the same tuple appear in the log,
the protocol records a synthetic `"fold"` envelope listing the attesting
pubkeys as collective signers and advances turn order. This prevents any
single peer from weaponising the timeout rule to eliminate a threatening stack.

**Chip accounting on dropout.** Chips committed to the pot in the current
street by a FOLDED_AUTO seat are not refunded — folding forfeits them,
consistent with a voluntary fold at the same moment. Chips still in the stack
at auto-fold time are preserved and returned at session end. A KICKED seat's
pre-kick commitments stay in the pot; its remaining stack is escrowed and
returned when the hand concludes.

The only legitimate full-refund path remains **unanimous signed abort**
(`action: "abort"` with every active seat's signature in `attestations`).
No other protocol path returns committed chips — which is, by design, what
closes the undo-button exploit described in the threshold-keys section above.

---

### 5. Client ↔ engine contract

This is the **local** boundary between a rendering client (the shipped
Godot front end, or the Tkinter harness) and the authoritative Python
engine running as an in-process object or a sidecar subprocess. It is
distinct from the peer wire format in §1: §1 is how *peers* talk to each
other over libp2p; §5 is how *one machine's* client talks to *its own*
engine. It is defined in Phase 1 because it is shared by every client and
because writing it down pins the engine boundary the whole architecture
leans on.

**Direction of the design.** The engine is authoritative and stateful.
The client is a pure function of the messages it receives: it holds no
game rules, computes no legality, and knows nothing it was not explicitly
sent. Three message kinds cross the boundary:

1. **Commands** — client → engine. A closed set of intents.
2. **Snapshots** — engine → client. The full renderable state for *one*
   seat, pushed after every state change.
3. **Events** — engine → client. A stream of discrete moments, used to
   trigger animations. Purely presentational; a client that ignores them
   still renders correctly from snapshots alone.

The transport for this boundary is deliberately unspecified here: for the
Tkinter harness it is direct method calls; for the Godot client it is
newline-delimited JSON over a local socket or stdio to the sidecar. The
message *shapes* below are identical in both cases.

**The hidden-information rule is a contract invariant, not a UI choice.**
A snapshot addressed to seat *N* MUST NOT contain any other seat's hole
cards. The engine already enforces this split today: table state is
broadcast to everyone (`game_state`), while hole cards are unicast only
to their owner (`deal_private`). §5 formalizes that separation as a
security property of the boundary — a client cannot leak what it was
never given.

**Command messages (client → engine).** The closed set the client may
send. Each is validated by the engine exactly as a peer action is (§1),
so an out-of-turn or illegal command is rejected, not trusted.

| command        | payload fields              | meaning                          |
|----------------|-----------------------------|----------------------------------|
| `fold`         | —                           | fold the current hand            |
| `check_call`   | —                           | check if free, else call to_call |
| `raise_to`     | `amount` (int)              | raise the total bet to `amount`  |
| `sit_out`      | —                           | sit out from the next hand       |
| `sit_in`       | `post_now` (bool)           | return; post blind now or wait   |
| `add_chips`    | `amount` (int)              | top up between hands             |
| `rabbit`       | —                           | request the fold-out runout      |
| `next_hand`    | —                           | acknowledge/deal the next hand   |

`raise_to` uses an absolute target (matching the engine's `act(i,
"raise", amount)` semantics and the bet slider), never a delta. The
engine clamps it to the legal band; the client never computes legality.

**Snapshot messages (engine → client).** Pushed after every state change,
addressed to one seat. Supersedes the current `game_state` broadcast by
folding in the per-seat and positional data the Tkinter GUI computes
locally today (so a socket client needs nothing but the snapshot to
render a full table):

```json
{
  "type": "snapshot",
  "seat": 3,
  "hand_num": 42,
  "street": "flop",
  "board": ["Ks", "7d", "2c"],
  "pot": 240,
  "side_pots": [{"amount": 120, "eligible": [0, 3]}],
  "button": 0, "sb_seat": 1, "bb_seat": 2,
  "action_on": 3,
  "seats": [
    {
      "seat": 0, "name": "P1", "stack": 980, "bet": 40,
      "folded": false, "all_in": false, "in_seat": true,
      "sitting_out": false, "last_action": "CALL 40",
      "pos": "BTN", "is_you": false
    }
  ],
  "you": {
    "hole": ["Ah", "Kh"],
    "legal": {
      "can_check": false, "to_call": 40, "can_raise": true,
      "min_to": 80, "max_to": 980
    }
  }
}
```

- `seats[*].last_action` and `pos` (SB/BB/BTN badge) are engine-derived,
  not client-inferred — that is the delta from today's `game_state`.
- `you.hole` is present **only** in the snapshot addressed to that seat
  (the §5 hidden-information invariant). Every other seat's `hole` is
  absent, never null-with-a-placeholder.
- `you.legal` mirrors the engine's `legal()` dict (`to_call`,
  `can_check`, `can_raise`, `min_to`, `max_to`); it is populated only
  when `action_on == seat`, and drives which buttons the client enables.

**Event messages (engine → client).** The animation stream. Each is a
discrete moment the client MAY render with a flourish; ignoring them still
leaves snapshots authoritative — a client that only consumes snapshots is
correct, just less animated.

The engine already emits a presentational event log today via
`emit(kind, text)` / `drain()`, with these kinds: `blind`, `bet`,
`raise`, `check`, `fold`, `street`, `show`, `pot`, `hand`. That log is
the seed of this stream. The contract's **target** event set below
normalizes those into structured payloads (text → fields) and adds the
few a rich client needs that the log does not yet distinguish
(`deal_hole`, `run_twice`). Growing `emit()` to carry structured payloads
for each of these is a Phase-1 task; until then the client can drive
basic animation from the existing `(kind, text)` log.

| target event  | payload                        | from emit kind / trigger       |
|---------------|--------------------------------|--------------------------------|
| `deal_hole`   | `seat`                         | (new) hole cards dealt         |
| `deal_board`  | `street`, `cards`              | `street`                       |
| `post_blind`  | `seat`, `kind`, `amount`       | `blind`                        |
| `bet`         | `seat`, `verb`, `amount`       | `bet` / `raise` / `check`      |
| `fold`        | `seat`                         | `fold`                         |
| `showdown`    | `reveals` (seat→cards), `best` | `show`                         |
| `award`       | `pot_index`, `winners`, `amt`  | `pot`                          |
| `run_twice`   | `board1`, `board2`             | (new) all-in board run twice   |

The Godot client's "surprisingly detailed" moments are a chosen subset of
these — `showdown` (the dimensional card flip), `award` (chip cascade),
`deal_board` — while the rest update instantly. The client decides which
events earn the deluxe treatment; the engine just reports that they
happened.

**Validation approach.** §5 is proven by driving the existing Tkinter GUI
and the P2P `Session` through this contract and asserting neither consumer
needs a field the snapshot/event set omits, plus a no-leak test: no
snapshot addressed to seat *N* ever contains another seat's `hole`. This
is the same house style as the engine suite — the contract is not "done"
until a test pins it.

---

## Phase 2 — Joint shuffle prototype

The four components below are the Phase 2 deliverable. All code runs in-process:
simulated peers exchange byte strings through function calls, with no network, no
sockets, and no threads. The canonical test harness provisions n simulated seats,
drives them through a full hand (DKG → shuffle → deal → showdown), and asserts the
properties defined in §4. Nothing here invalidates Phase 1; the `deal_step` action
introduced in §1.1 of Phase 1 is fully specified in §2.1 below.

> **Design revision (2026-07-19, after external cryptographic review) —
> shuffle-proof construction changed.** The Bayer–Groth argument
> originally specified in §2.2 is **parked**: its sublinear proof size
> buys nothing at N=52, and its five nested sub-arguments are exactly
> where implementation soundness bugs hide (see the 2019 Scytl/Swiss
> Post breaks). The adopted design, in deployment order:
>
> 1. **Verifiable round 0** — the shuffle chain starts from the
>    *trivial* deck (`elgamal.make_trivial_deck`, E(M;0) = (identity,
>    M)), checkable by inspection (`verify_trivial_deck`), so the chain
>    provably begins with the 52 canonical cards. *Implemented.*
> 2. **Post-hand full-deck audit** (`holdem/p2p/deck_audit.py`) — at
>    hand end every seat publishes DLEQ-proven decryption shares for all
>    52 positions; everyone checks the plaintext multiset equals the
>    canonical deck. Covert security: substitution, duplication, and
>    drops are *always detected*, the hand is void, a lying decryptor
>    is identified by seat, and a corrupt deck is attributed to the
>    exact shuffler round via the chain walk (`first_corrupt_round`).
>    Consequence, accepted: mucked and burned cards become public at
>    hand end. *Implemented.*
> 3. **Shadow-deck cut-and-choose shuffle proof, k=128** (Sako–Kilian
>    style) — adds *prevention* on top of detection. k shadow shuffles
>    per real shuffle; Fiat–Shamir challenge bits over the complete
>    transcript; each bit opens either the shadow's own (σⱼ, rⱼ) or the
>    bridge φⱼ = σⱼ⁻¹∘π with re-encryption deltas. Soundness is a
>    one-paragraph composition argument (2⁻ᵏ per hash query); k is a
>    **full** security parameter under Fiat–Shamir grinding, not a
>    statistical one — do not reduce it to 40. No commitment key
>    exists, so the Scytl trapdoor bug class is structurally
>    impossible. ~650 KB per proof — in budget per the broadband-only
>    scope decision. *Planned next.*
> 4. **Schnorr proof-of-possession per key share at the DKG** — closes
>    the rogue-key attack (the last announcer choosing
>    Xₙ = X* − ΣXᵢ to own the joint key alone). *Planned.*
>
> If a compact algebraic proof is ever wanted later, the pick is
> **Terelius–Wikström** (with Verificatum as an audited reference), not
> Bayer–Groth or Neff. The Pedersen commitment (`pedersen.py`, NUMS
> generators) and the single-value product argument (`bg_svp.py`) built
> for the BG path are TW prerequisites and remain as tested code. The
> §2.2 Bayer–Groth text below is retained as reference for that
> contingency and is not the implementation target.

---

### 1. Cipher suite and deck encoding

**Group.** All public-key operations use **Ristretto255**, the prime-order group of
order q = 2²⁵² + 27742317777372353535851937790883648493 constructed over Curve25519.
Ristretto encodes group elements unambiguously as 32-byte strings, sidesteps the
cofactor subtleties that afflict the raw Curve25519 group, and is supported by the
`ristretto255` Python package. Every point in this protocol is transmitted as a
64-character hex string (32 bytes, lowercase).

**ElGamal encryption.** A ciphertext is a pair (C0, C1), both Ristretto255 points:

- **Encrypt.** Choose a random scalar r ∈ [1, q−1]; compute C0 = r·G and
  C1 = M + r·PK, where G is the Ristretto255 base point and PK is the joint
  public key produced by the Phase 1 DKG handshake.
- **Re-encrypt.** Given (C0, C1) and a fresh scalar r′, compute
  (C0 + r′·G, C1 + r′·PK). The underlying plaintext is unchanged; this is
  homomorphic re-randomisation, and it is how each seat's shuffle round works.
- **Partial decrypt.** Seat i contributes Di = xᵢ·C0, where xᵢ is its DKG
  private share.
- **Full decrypt.** M = C1 − Σ Dᵢ, summing the partial contributions of every
  participating seat. (Minus is group subtraction: add the negation of the sum.)

**Deck encoding.** Fifty-two cards must map injectively to Ristretto255 points in a
way that is deterministic, public, and free of known discrete-log relations between
cards. The encoding is fixed for all time; no per-hand negotiation is needed.

```python
import ristretto255 as rist

SUITS = "cdhs"           # clubs, diamonds, hearts, spades
RANKS = "23456789TJQKA"
CARDS = [r + s for s in SUITS for r in RANKS]   # 52 strings, canonical order

def card_point(card: str) -> bytes:
    """Return the canonical 32-byte Ristretto255 encoding for a card label."""
    idx = CARDS.index(card)                       # 0..51
    label = f"poker.card.v1:{idx}:{card}".encode()
    return rist.hash_to_group(label)              # RFC 9380 / hash_to_ristretto255
```

`rist.hash_to_group` applies the Elligator2 + Ristretto hash-to-curve construction
specified in RFC 9380, producing a uniformly distributed point with no known
discrete-log relation to G or to any other card point. The 52 resulting points are
pre-computed once at module load and cached; they are invariant across hands and
sessions and may be published as a test vector.

---

### 2. Sequential shuffle and re-encryption

**Overview.** Once the DKG handshake completes, every active seat takes a shuffle
turn. Seat 0 (the hand initiator) goes first: it encrypts each of the 52 card points
under the joint public key PK with independent random scalars, producing the initial
deck. Seats then shuffle in ascending seat order. Each shuffle round applies a secret
permutation to the deck and re-encrypts every ciphertext with a fresh random scalar.
After all n seats have shuffled, the deck is locked under every player's share in an
order nobody knows — no single player, and no coalition smaller than t, can infer
which ciphertext corresponds to which card.

**Initial encryption (seat 0).**

```python
def make_initial_deck(pk: bytes) -> list[tuple[bytes, bytes]]:
    """Encrypt each of the 52 card points under the joint public key pk."""
    deck = []
    for card in CARDS:
        r = rist.random_scalar()
        C0 = rist.mul(r, rist.G)
        C1 = rist.add(card_point(card), rist.mul(r, pk))
        deck.append((C0, C1))
    return deck   # 52 (C0, C1) pairs; each element is a 32-byte bytes object
```

Seat 0 broadcasts this initial deck as its `deal_step` envelope (round 0) and
simultaneously produces a verifiable-shuffle proof (§2.2) relative to the trivially
ordered identity permutation, making even the first mover's action auditable.

**Shuffle round.** Each seat i, on receiving the previous round's deck from the log,
applies a secret permutation πᵢ and re-encrypts every card with an independent fresh
scalar, then broadcasts the resulting deck together with a proof:

```python
import secrets

def shuffle_deck(
    prev: list[tuple[bytes, bytes]],
    pk: bytes,
) -> tuple[list[tuple[bytes, bytes]], list[bytes], list[int]]:
    """
    Returns:
        next_deck  – 52 re-encrypted, permuted ciphertexts
        r_scalars  – 52 fresh re-encryption scalars (kept secret; needed for proof)
        perm       – the secret permutation as a list of source indices (never sent)
    """
    perm = list(range(52))
    secrets.SystemRandom().shuffle(perm)
    next_deck, r_scalars = [], []
    for src in perm:
        C0, C1 = prev[src]
        r = rist.random_scalar()
        next_deck.append((
            rist.add(C0, rist.mul(r, rist.G)),
            rist.add(C1, rist.mul(r, pk)),
        ))
        r_scalars.append(r)
    return next_deck, r_scalars, perm
```

`r_scalars` and `perm` are kept in the seat's local memory and never transmitted.
What goes onto the wire is the shuffled deck and a zero-knowledge verifiable-shuffle
proof that binds the output to the input without disclosing either.

**`deal_step` wire format.** The `deal_step` action type (declared in Phase 1 §1.1
with payload TBD) carries all shuffle and reveal messages. Its payload is typed by a
`"step"` field:

```json
{
  "step":  "shuffle",
  "round": 0,
  "deck": [
    ["<C0 hex>", "<C1 hex>"],
    "..."
  ],
  "proof": "<hex-encoded verifiable-shuffle proof blob>"
}
```

- **`step`** — one of `"shuffle"` (§2), `"partial_decrypt"` (§3), or
  `"partial_decrypt_reconstruct"` (§3.3).
- **`round`** — shuffle round index. Round 0 is seat 0's initial encryption; rounds
  1 through n are the per-seat shuffles in seat order.
- **`deck`** — array of exactly 52 two-element arrays, each `[C0_hex, C1_hex]`.
  Both points must be valid, canonical Ristretto255 encodings; receivers reject any
  envelope containing a non-canonical or low-order point.
- **`proof`** — the verifiable-shuffle proof blob (§2.2), hex-encoded.

The entire envelope is signed and hash-chained exactly like any other action. Because
the log is append-only, the input deck for round k is unambiguously the `deck` field
of the round k−1 entry at its logged position; no out-of-band reference is needed.

**Shuffle order and completion.** Seats shuffle in ascending seat-number order.
Completion is defined as n+1 valid `deal_step`/`"shuffle"` entries in the log:
seat 0's initial encryption (round 0) followed by one shuffle per seat (rounds 1..n).
A seat that fails to produce its shuffle entry within the DKG step timeout (TABLE_RULE,
default 10 s) is KICKED per Phase 1 §4. The remaining seats co-sign a DKG restart
with n′ = n − 1 and the shuffle sequence restarts from round 0. A seat that triggers
two restarts within one hand is barred from future hands under that pubkey, as with
the DKG re-run limit.

---

### 2.2 Verifiable-shuffle proof

A seat that shuffles the deck must prove the output is a re-encryption of some
permutation of the input without revealing which permutation or which re-encryption
scalars it used. Producing a forged proof must be computationally infeasible: a
forgery would allow a seat to swap in card points not present in the input, changing
the deck's contents undetectably.

**Construction.** The proof follows the Bayer–Groth (2012) argument for ElGamal
shuffles over a prime-order group, instantiated over Ristretto255. It proceeds in two
parts.

*Permutation commitment.* The shuffler commits to its permutation π by publishing a
commitment vector (c₀, …, c₅₁) where each cₖ = π(k)·h + ρₖ·G, with ρₖ a random
blinding scalar and h a publicly fixed second base point: `h =
hash_to_ristretto255("poker.shuffle.h.v1")`. The commitment is perfectly hiding
(ρₖ is never sent) and computationally binding (opening requires solving a discrete log).

*Product argument.* The shuffler proves — using a series of challenge-response rounds
derived by Fiat-Shamir — that the committed permutation and the claimed re-encryption
scalars jointly transform the input deck into the output deck. The Fiat-Shamir
challenge is derived as:

```python
import hashlib, json

def shuffle_challenge(
    prev_deck: list[tuple[bytes, bytes]],
    next_deck: list[tuple[bytes, bytes]],
    commits:   list[bytes],
) -> bytes:
    def encode_deck(d):
        return [[c.hex() for c in pair] for pair in d]

    preimage = json.dumps({
        "tag":      "poker.shuffle.challenge.v1",
        "prev":     encode_deck(prev_deck),
        "next":     encode_deck(next_deck),
        "commits":  [c.hex() for c in commits],
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(preimage).digest()
```

Receivers re-derive the challenge from the two decks already in the log (the previous
round's `deck` field and the current one) plus the commitment vector in the proof
blob, then verify all response equations. No additional trust is required. A single
verification pass costs O(|deck|) group multiplications — well under 1 s for 52 cards
on current hardware.

**Proof blob encoding.**

```
4  bytes (LE uint32) — total blob length in bytes
52 × 32 bytes        — permutation commitment vector (c₀…c₅₁), Ristretto255 points
32 bytes             — Fiat-Shamir challenge (SHA-256 output)
52 × 32 bytes        — per-card response scalars
64 bytes             — two auxiliary response scalars for the product argument
──────────────────────────────────────────────────────────────────
Total: 4 + 1664 + 32 + 1664 + 64 = 3 428 bytes per shuffle proof
```

A receiver that cannot verify a shuffle proof broadcasts a signed `"chain_fault"`
(Phase 1 §2) referencing the offending `deal_step` entry, naming the specific failing
verification equation, and including the Fiat-Shamir preimage it recomputed. The
faulty shuffler is immediately KICKED.

---

### 3. Selective decryption for dealing

Once the shuffle is complete, the deck is a list of 52 ciphertexts in an unknown
permutation. The game engine assigns card positions to seats (two hole cards each,
then community cards) by index into that list. Decryption is cooperative and
selective: who contributes partial decryptions determines who learns the plaintext.

**Partial decryption shares.** To reveal the card at encrypted position k, every
seat i that is *not* the sole recipient contributes:

```python
def partial_decrypt(C0: bytes, xi: bytes) -> bytes:
    """Compute Dᵢ = xᵢ · C0, seat i's partial decryption share."""
    return rist.mul(xi, C0)
```

Seat i broadcasts this share as a `deal_step` envelope:

```json
{
  "step":       "partial_decrypt",
  "card_index": 7,
  "recipient":  2,
  "share":      "<Dᵢ hex, 32 bytes>",
  "proof":      "<DLEQ proof hex, 64 bytes>"
}
```

- **`card_index`** — index into the shuffled deck (0–51).
- **`recipient`** — seat number of the player who will decrypt, or `null` for a
  community card where all seats are recipients simultaneously.
- **`share`** — the partial decryption point Dᵢ = xᵢ · C0.
- **`proof`** — a DLEQ (discrete-log equality) proof that the same scalar xᵢ was
  used to compute both Dᵢ = xᵢ · C0 and the seat's DKG public-key share
  Xᵢ = xᵢ · G. This prevents a seat from submitting a garbage share while appearing
  to cooperate.

**DLEQ proof.** The proof is a standard Chaum-Pedersen sigma protocol, compressed
by Fiat-Shamir:

```python
def dleq_prove(xi: bytes, C0: bytes, G: bytes) -> bytes:
    k  = rist.random_scalar()
    R1 = rist.mul(k, G)               # k · G
    R2 = rist.mul(k, C0)              # k · C0
    Xi = rist.mul(xi, G)              # public key share
    Di = rist.mul(xi, C0)             # partial decrypt

    ch_input = b"poker.dleq.v1|" + G + Xi + C0 + Di + R1 + R2
    c  = int.from_bytes(hashlib.sha256(ch_input).digest(), "little") % rist.Q
    s  = (int.from_bytes(k, "little") - int.from_bytes(xi, "little") * c) % rist.Q
    return c.to_bytes(32, "little") + s.to_bytes(32, "little")   # 64 bytes

def dleq_verify(Xi: bytes, Di: bytes, C0: bytes, G: bytes, proof: bytes) -> bool:
    c  = int.from_bytes(proof[:32], "little")
    s  = int.from_bytes(proof[32:], "little")
    R1 = rist.add(rist.mul(s, G),  rist.mul(c, Xi))   # s·G + c·Xᵢ
    R2 = rist.add(rist.mul(s, C0), rist.mul(c, Di))   # s·C0 + c·Dᵢ
    ch_input = b"poker.dleq.v1|" + G + Xi + C0 + Di + R1 + R2
    return c == int.from_bytes(hashlib.sha256(ch_input).digest(), "little") % rist.Q
```

A seat that broadcasts a partial decryption with an invalid DLEQ proof is KICKED. A
seat that simply omits its required partial decryption within the dealing timeout
(equal to the DKG step timeout, TABLE_RULE default 10 s) is treated as a TIMED_OUT
action and auto-folded per §4 of Phase 1 — except that a seat which is already
ALL_IN_ABSENT bypasses auto-fold and instead triggers threshold reconstruction (§3.3).

**Final decryption by the recipient.** Once seat j holds all n−1 valid partial
decryption shares from the other seats, it combines them with its own contribution:

```python
def final_decrypt(
    C0: bytes,
    C1: bytes,
    xj: bytes,
    others: list[bytes],   # Dᵢ = xᵢ · C0 for each i ≠ j
) -> bytes:
    """Return the plaintext point M."""
    own  = rist.mul(xj, C0)           # seat j's own contribution
    total = own
    for D in others:
        total = rist.add(total, D)
    return rist.sub(C1, total)         # M = C1 − Σ xᵢ · C0
```

The result is a 32-byte Ristretto255 point. The recipient looks it up in the
pre-computed card-point table. If no card matches — indicating the deck was
maliciously constructed during a shuffle — the recipient broadcasts a `"chain_fault"`
referencing the shuffle `deal_step` that introduced the anomalous ciphertext, and the
faulty shuffler is KICKED.

The recipient never publishes the plaintext point during normal play. The card is
known only to seat j until showdown or voluntary reveal, provided no coalition of t
or more other seats combines their DKG shares to reconstruct xj — the same
liveness-privacy tradeoff as the DKG threshold.

**Community card reveal.** For a community card (flop, turn, river), all n seats
contribute their partial decryption shares. Because every share appears in the log,
any observer can assemble M. The payload is identical to the hole-card
`"partial_decrypt"` step except `"recipient"` is `null`. The game engine triggers
this dealing step in sequence: all five community cards are treated as successive
cooperative decryption rounds, each producing one card point visible to all.

**Mucking.** A seat that folds and declines to reveal its hole cards at showdown
simply never requests partial decryptions for those card positions. The ciphertexts
remain opaque in the log permanently. No information about the mucked cards leaks
from the transcript.

**Showdown reveal.** A seat wishing to claim a portion of the pot at showdown
broadcasts a `"showdown_declare"` envelope (new action type defined below), listing
the encrypted deck positions of its hole cards. The remaining active seats respond
with `deal_step`/`"partial_decrypt"` envelopes for each listed position. The
declaring seat publishes the recovered plaintext points as a final confirmation
envelope. Failure to produce a valid final confirmation within the dealing timeout
voids the declare; the seat's hand is treated as mucked.

New action type added to the Phase 1 §1.1 action-type list:

- **`"showdown_declare"`** — seat announces intent to reveal hole cards at showdown.
  Payload: `{"seat": <int>, "card_indices": [k₁, k₂]}`, where k₁ and k₂ are the
  encrypted deck positions assigned to that seat during the deal phase.

---

### 3.3 Threshold reconstruction for absent seats

A seat in state ALL_IN_ABSENT or FOLDED_AUTO cannot contribute partial decryption
shares. For hole cards belonging to that seat, active seats substitute threshold
reconstruction for its direct participation.

The DKG output includes, for each seat j, encrypted share fragments from the other
seats (the `Sᵢⱼ` values broadcast during the `dkg_reveal` phase). Any t = ⌈2n/3⌉
active seats can reconstruct seat j's private DKG share xⱼ by Lagrange interpolation
over the Shamir shares they each received:

```python
def reconstruct_share(
    seat_j:  int,
    t_shares: dict[int, int],   # {seat_i: Sᵢⱼ} for t distinct active seats
    q:        int,
) -> int:
    """Return xⱼ mod q via Lagrange interpolation."""
    nodes = list(t_shares.keys())
    acc   = 0
    for i in nodes:
        num = den = 1
        for m in nodes:
            if m != i:
                num = (num * (0 - m - 1)) % q
                den = (den * (i - m)) % q
        coeff = (num * pow(den, q - 2, q)) % q
        acc   = (acc + t_shares[i] * coeff) % q
    return acc
```

Once xⱼ is reconstructed, the designating seat (lowest active seat number by
convention) does *not* publish xⱼ directly. Instead it computes and broadcasts the
partial decryption Dⱼ = xⱼ_reconstructed · C0 for each required card position, with
a DLEQ proof demonstrating consistency with seat j's public DKG commitment Xⱼ. The
remaining active seats verify the proof and countersign a `timeout_attest` referencing
the `deal_step` entry, establishing t-of-active-seats consensus that the reconstruction
is correct before it is accepted as a valid partial decryption. The envelope that
carries a reconstructed share uses step type `"partial_decrypt_reconstruct"` in place
of `"partial_decrypt"`, and adds a field `"reconstructed_for": <seat_j>`.

**Privacy under reconstruction.** The t seats that perform reconstruction learn xⱼ
in full, giving them the ability to read all of seat j's hole cards for this hand. As
stated in the threshold-keys section of the main document, this is the unavoidable
liveness-privacy dial: the reconstruction threshold is chosen so that a coalition
large enough to rescue a hand from a dropout is also large enough that it could have
broken hole-card privacy anyway through the DKG shares alone. The protocol cannot
improve on this bound.

---

### 4. Test harness and no-leak invariant

Phase 2 is — as the roadmap notes — the single hardest piece in the project. Library
support for verifiable ElGamal shuffles over Ristretto255 is sparse; much of the
implementation derives directly from Bayer–Groth (2012) and the Barnett–Smart (2003)
exposition. The test suite compensates with aggressive property-based and adversarial
coverage; passing it is the definition of a correct Phase 2 implementation.

All tests run in-process. Peers are simulated as objects sharing a list representing
the action log; signing and verification use real Ed25519 keys generated fresh per
test run. No network layer exists at this phase.

**Property 1 — Replay determinism.** Fix the PRNG seed for every seat's shuffle
scalars, permutation, and DKG randomness. A full hand driven with the same seeds must
produce a byte-for-byte identical action log on every run. Tested by running each
scenario twice and asserting `SHA-256(log₁) == SHA-256(log₂)`. Any non-determinism
in the cipher suite or proof construction is a bug, not a property to tolerate.

**Property 2 — Shuffle soundness (adversarial oracle).** A shuffler that produces
output not corresponding to any valid permutation + re-encryption of its input must
generate a proof that fails verification. The test harness injects malformed
`deal_step`/`"shuffle"` entries covering at least the following eight variants:
(a) deck permuted but not re-encrypted; (b) deck re-encrypted but not permuted;
(c) deck with one card replaced by a fresh encryption of a different card;
(d) proof commitment vector mismatched to actual permutation; (e) Fiat-Shamir
challenge tampered; (f) response scalars zeroed; (g) proof copied verbatim from a
previous round (replay); (h) proof generated for a different input deck. Every active
peer must detect each fault within one verification pass and broadcast a `chain_fault`.

**Property 3 — Correct full decryption.** After a clean n-seat shuffle with known
seeds, the test oracle knows the composition of all permutations and can compute the
expected card at every deck position. Cooperative decryption of all 52 positions must
recover the expected card point at each. Any mismatch indicates a bug in the shuffle,
the partial-decrypt arithmetic, or the deck encoding.

**Property 4 — No-leak assertion.** This is the canonical privacy test and the most
important property in the suite. For each seat j, extract every log entry that is not
seat j's own final-decrypt output for a hole card assigned to j, and assert that none
of those entries, when serialised to bytes, contains the hex encoding of either of
seat j's hole-card plaintext points:

```python
import json, hashlib

def assert_no_leak(
    log:          list[dict],
    seat_j:       int,
    j_pubkey:     str,
    hole_points:  list[bytes],
) -> None:
    hole_hexes = {p.hex() for p in hole_points}
    for entry in log:
        env = entry["envelope"]
        payload = env.get("payload", {})
        # Skip the entry where seat j publishes its own recovered plaintext
        if (env["action"] == "deal_step"
                and payload.get("step") == "partial_decrypt"
                and payload.get("recipient") == seat_j
                and env["pubkey"] == j_pubkey):
            continue
        blob = json.dumps(entry, sort_keys=True, separators=(",", ":"))
        for h in hole_hexes:
            assert h not in blob, (
                f"PRIVACY VIOLATION: card {h} for seat {seat_j} "
                f"leaked in entry seq={env.get('seq')}"
            )
```

Run for every seat across every test scenario. A failure is a protocol design flaw,
not an implementation bug, and is an unconditional release blocker.

**Property 5 — Threshold dropout recovery.** With n seats and threshold t, simulate
the simultaneous dropout of n − t seats at each of the following moments:
(a) after the shuffle is complete but before any hole cards are dealt;
(b) after hole cards are dealt but before the flop;
(c) immediately before showdown. Assert that in each case: the remaining t seats
complete threshold reconstruction of the absent seats' partial decryptions; all
community cards and the absent seats' hole cards are recovered to the correct values;
and chip accounting satisfies conservation (total chips in stacks plus pots equals
the session buy-in sum) with the absent seats treated as having folded or, if already
all-in, as eligible for the side pot.

**Coverage requirements.** The test suite must reach 100% branch coverage on the
cipher-suite module (§1), the shuffle-and-proof module (§2), and the
selective-decryption module (§3). The shuffle-soundness adversarial test (Property 2)
must exercise all eight specified malformed-proof variants, confirmed by checking that
each injects a `chain_fault` entry into the log. Property 4 must run for every seat
across a minimum 6-seat hand scenario (producing at least 12 hole-card privacy
assertions per run). Property 5 must be exercised at all three dropout timing points
for n = 4 seats (t = 3) and n = 6 seats (t = 4).

---

## Phase 3 — Transport

The Phase 3 deliverable is a working P2P transport layer that lets strangers find
each other, establish connections, and exchange signed game messages with no server
we operate and no configuration from the user. The Phase 1 action log is the payload;
the transport carries it as opaque signed bytes. Nothing in this section touches the
cryptographic protocol.

---

### 1. Why libp2p

The design goal is **"download and play"**: a user installs the app and joins a
table with strangers without typing an IP address or running a server. Two transport
requirements fall out of this directly.

*Peer discovery without a central authority.* Strangers must be able to find open
tables without a matchmaking server we run and pay for indefinitely. A DHT is the
right tool: it is a public commons where any node contributes capacity, no single
operator is authoritative, and the directory scales with the user base rather than
with our infrastructure spend.

*NAT traversal without user configuration.* Most home connections sit behind NAT.
Players should not need to forward ports or touch a router. The standard fix —
TURN relays — requires a relay fleet we operate. libp2p's circuit relay v2 model
achieves the same result without a dedicated server: any node in the swarm that
supports the relay protocol can serve as a relay, and that node is just another app
user.

libp2p satisfies both requirements in a single stack. Its Kademlia DHT handles
discovery; circuit relay v2 handles NAT traversal; DCUtR (Direct Connection Upgrade
through Relay) attempts a direct hole-punch before committing to a relayed path. The
whole stack is protocol-agnostic: it carries byte streams, which maps cleanly onto the
Phase 1 signed action envelopes.

WebRTC was the prior primary candidate. It was removed because every practical WebRTC
deployment requires a signalling server for SDP exchange and a TURN server for
symmetric NAT — both of which must be operated indefinitely. libp2p's circuit relay
achieves the same NAT traversal with no dedicated server fleet: relays are contributed
by peers in the swarm, and the relay sees only encrypted signed bytes it cannot
interpret.

Reticulum (mesh-radio / offline LAN transport) was also evaluated and is **retired
per the broadband-only scope decision** recorded in the Transport section above.
The anti-cheat protocol's bandwidth budget is set for broadband; narrowband links
are not a design constraint.

---

### 2. Peer identity

On first launch the app generates an **Ed25519 keypair** and persists it to the
user's data directory. This keypair serves two roles simultaneously.

*Protocol identity (Phase 1).* The `pubkey` field in every signed action envelope;
the signing key for all game messages. Defined in Phase 1 §1.

*libp2p peer ID.* libp2p derives a peer ID from the public key using the standard
`multihash("identity", pubkey_bytes)` encoding. The peer ID is therefore the
player's cryptographic identity at both the game-protocol layer and the network
layer — no separate key material to manage, no second onboarding step.

Generating the keypair on first launch is the full "setup" the user performs. The
keypair is stable across sessions so that a reconnecting player presents the same
peer ID and pubkey the other seats already have in their logs.

```python
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import base64, json, pathlib

def load_or_create_identity(path: pathlib.Path) -> Ed25519PrivateKey:
    """Load the persisted keypair, or generate and save a fresh one."""
    if path.exists():
        raw = json.loads(path.read_text())
        return Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(raw["ed25519_private"])
        )
    key = Ed25519PrivateKey.generate()
    path.write_text(json.dumps({
        "ed25519_private": base64.b64encode(
            key.private_bytes_raw()
        ).decode()
    }))
    return key
```

The corresponding 32-byte public key, hex-encoded, is the `pubkey` used in Phase 1
envelopes. The libp2p peer ID is derived from the same bytes; the two are
interchangeable given knowledge of the other.

---

### 3. Lobby and game discovery

Open tables are advertised on the DHT under a **topic key** derived from the join
code. The join code is a short human-readable string (e.g. `"RIVER-7"`) that a
table host shares out-of-band with specific players, or publishes to the open lobby.
It encodes the rules hash so every peer that finds the table can verify it is playing
the same game.

```python
import hashlib

def table_topic(join_code: str) -> bytes:
    """DHT key under which a table's peer multiaddrs are advertised."""
    return hashlib.sha256(b"poker.table.v1:" + join_code.encode()).digest()
```

The table host calls `dht.provide(table_topic(join_code))` to announce the table.
A joining player calls `dht.find_providers(table_topic(join_code))` to retrieve the
peer IDs and multiaddrs of the host (and any other players already seated), then dials
them directly.

For **open lobby discovery** — a player browsing for a game rather than entering a
specific join code — a well-known global topic is used:

```python
LOBBY_TOPIC = hashlib.sha256(b"poker.lobby.v1").digest()
```

Any host advertising an open table additionally provides `LOBBY_TOPIC`. A browsing
player calls `dht.find_providers(LOBBY_TOPIC)`, receives a set of peer IDs running
open tables, dials each over the `/poker/lobby/1.0.0` protocol, and requests table
metadata. The response is a single signed envelope:

```json
{
  "v":          1,
  "action":     "table_info",
  "pubkey":     "<host pubkey>",
  "seq":        0,
  "ts":         0,
  "payload": {
    "join_code":   "RIVER-7",
    "rules_hash":  "<10-hex>",
    "seats_total": 6,
    "seats_taken": 2
  },
  "sig": "<sig>"
}
```

The browsing player verifies the signature, displays the table list, and dials the
chosen table. No matchmaking server is involved at any step.

---

### 4. Connection establishment

The dial sequence for joining a table is:

1. **Derive the topic key** from the join code: `table_topic(join_code)`.
2. **Resolve providers** via `dht.find_providers(topic_key)`. The DHT returns a
   list of `(peer_id, multiaddrs)` pairs for peers already at the table.
3. **Attempt direct dial** to each multiaddr in preference order (QUIC preferred
   over TCP for lower latency and built-in encryption). Direct dial succeeds for
   peers behind full-cone NAT or with a public address.
4. **DCUtR hole-punch** if direct dial fails. The relay coordinates a
   simultaneous-open between the two peers; this succeeds for a majority of
   symmetric NAT configurations and removes the relay from the data path when
   it works.
5. **Circuit relay fallback** if DCUtR fails. The joining peer asks a relay-capable
   node in the swarm (discovered via the DHT's `circuit-relay-v2` provider
   advertisement) to relay the connection. The relay forwards encrypted signed
   bytes and cannot read game content.
6. **Open a game stream** on the `/poker/game/1.0.0` protocol once the underlying
   connection is established.

The relay supply scales with the player base: any peer with a public address or
full-cone NAT that has been running the app is a potential relay. No relay fleet
needs to be operated or paid for.

---

### 5. Message transport

Once a `/poker/game/1.0.0` stream is open, signed action envelopes from Phase 1
flow over it as length-prefixed binary frames:

```
4 bytes (big-endian uint32) — frame length N
N bytes                     — UTF-8 JSON of the signed envelope
```

The transport carries opaque signed bytes. It does not validate signatures, does
not interpret action types, and does not reorder messages beyond what the underlying
QUIC or TCP stream guarantees. All game logic — signature verification, chain
linkage, turn-order enforcement, FSM transitions — is handled by the Phase 1 codec
above the transport layer. The transport is untrusted by design; a compromised relay
or a man-in-the-middle cannot forge a valid signed envelope.

**Stream multiplexing.** Each peer pair maintains a single multiplexed connection
(yamux or mplex). Game streams (`/poker/game/1.0.0`) and lobby streams
(`/poker/lobby/1.0.0`) are distinct stream IDs within that connection. A reconnecting
peer dials the same peer IDs, opens a new game stream, and sends a Phase 1
`session_resume` envelope as the first message.

**Broadcast model.** At a table of n ≤ 9 players, each peer maintains direct (or
relayed) connections to every other peer — a full mesh. When a peer sends a signed
action envelope, it sends it directly to all n−1 peers. There is no routing hop; no
peer is in a privileged "hub" position. For n ≤ 9 the fan-out cost is negligible, and
the full mesh means that no single peer's dropout can partition the table.

---

### 6. py-libp2p vs Go libp2p sidecar

Two implementation paths are available in Python.

**py-libp2p** is a native Python implementation. It covers Kademlia DHT, QUIC,
circuit relay v2, and yamux, and has matured considerably since its initial
development. The advantage is a single-language stack: no subprocess, no IPC, no
cross-platform binary bundling. The disadvantage is maturity relative to the Go
implementation: py-libp2p sees less adversarial production use, some protocol
versions lag the specification, and performance under load is untested for this
use case.

**Go libp2p sidecar** spawns a small Go binary as a subprocess. The Go implementation
is the reference: it is used in production by IPFS, Filecoin, and Ethereum's consensus
layer. The sidecar exposes a thin local interface over a Unix socket or named pipe:

```
Operations: dial, listen, send, recv, dht_provide, dht_find_providers
Protocol: newline-delimited JSON-RPC
```

The Python app calls these six operations; the sidecar handles all libp2p protocol
details. The IPC layer is intentionally minimal — under 200 lines of Go for the
sidecar server and under 200 lines of Python for the client shim. The disadvantage
is distribution: the sidecar binary must be compiled for each target platform
(Windows, macOS, Linux × amd64/arm64) and bundled with the installer.

**Decision rule.** Start with py-libp2p. Gate the decision on a specific integration
test: two peers behind simulated symmetric NAT must establish a game stream (via
circuit relay or DCUtR) and exchange 1 000 signed action envelopes without loss,
and the open lobby discovery must work across a 5-peer local DHT. If py-libp2p
passes those tests, it ships. If it fails on circuit relay or DHT stability, switch
to the Go sidecar. The Phase 1 interface boundary — signed byte strings in, signed
byte strings out — means the switch touches only the transport module, not the game
protocol or test harness.

---

### 4. Internet play — Phase 3.5 limitation

**Current status (Phase 3 implementation):** the transport uses asyncio TCP with
LAN multicast rendezvous (UDP group `239.255.77.77:7777`). This works automatically
for players on the same local network segment. Two players on different networks
(different homes, offices, etc.) cannot reach each other via multicast.

**Workaround until Phase 3.5:** the host shares their LAN or public IP address and
listen port manually. The "Join Game" dialog has an optional *Host address override*
field (`host:port`) for exactly this purpose. Steps:

1. Host clicks **Create Game** — the dialog shows the LAN listen address
   (e.g. `0.0.0.0:41337`; the actual port is OS-assigned).
2. Host finds their public IP (e.g. via `api.ipify.org`) and forwards the
   listen port through their router.
3. Host shares the **room code** (for identity verification) *and*
   **public-ip:port** (for routing) with the joiner out-of-band.
4. Joiner pastes the room code, enters `public-ip:port` in the override
   field, and clicks **Connect**.

**Phase 3.5 plan:** wire STUN (`stun.l.google.com:19302`) to discover the public
IP automatically, add DCUtR hole-punching for symmetric NAT traversal, and fall back
to a community-contributed circuit relay for cases where hole-punching fails. Once
this is complete the address-override field becomes unnecessary for internet play and
the multicast rendezvous becomes the LAN fast-path only.

---

## Phase 4 — Multiplayer client and packaging

Phase 4 is the integration and distribution phase. The Phase 1 signed
action log and the Phase 3 transport are wired together into a single
runnable application that a user can download and play without installing
Python or configuring anything.

> **Client scope note (supersedes the code below).** The shipped front
> end is a **native Godot 2.5D client**, not Tkinter (see "Client
> direction (decided)" in the roadmap above). The Python side described
> in this section — libp2p node startup, DHT table discovery, wiring the
> signed action log and mental-poker shuffle — is the **authoritative
> sidecar** the Godot client drives, and remains correct as written. The
> `tk.Tk()` snippets below are reference/harness form: read them as "the
> sidecar exposes this; the Godot client calls it over the client↔engine
> boundary." The Tkinter GUI stays as the engine's dev/test harness. A
> web/browser client is permanently out of scope.

---

### 1. libp2p node startup on launch

When the application starts, before the onboarding screens appear, it
initialises a libp2p node in a background thread:

```python
import threading
from holdem.p2p import start_node   # Phase 4 module

def main():
    node = start_node()          # generates or loads keypair; binds a port
    threading.Thread(target=node.run, daemon=True).start()
    root = tk.Tk()
    ...
```

`start_node()` calls `load_or_create_identity()` (Phase 3 §2), binds a
QUIC listener on an OS-assigned ephemeral port, and returns a node
handle. All subsequent DHT and connection operations go through this
handle. The node runs for the life of the process; no teardown step is
needed because it is a daemon thread. The node handle is injected into
`OnboardingFlow` and `Holdem` as a constructor argument so neither class
imports networking directly.

---

### 2. DHT bootstrap peers

The Kademlia DHT is useless until the node has at least one peer to
contact. The application ships with a small hardcoded bootstrap list —
well-known long-running nodes, operated by early users with stable
public IPs rather than by a single authority:

```python
BOOTSTRAP_PEERS = [
    "/ip4/51.158.75.17/tcp/4001/p2p/QmBootstrap1...",
    "/ip4/178.62.244.176/tcp/4001/p2p/QmBootstrap2...",
    # ...
]
```

On startup, `start_node()` dials each bootstrap peer in parallel and
performs a Kademlia bootstrap walk (find_node queries toward the local
peer ID) to populate its routing table. The walk runs in the background;
the lobby UI appears immediately and shows a "Connecting…" status label
until the DHT is ready. If no bootstrap peer is reachable (offline LAN
mode), the node falls back to mDNS local discovery — sufficient for a
home-network game, and requiring no configuration from the user.

---

### 3. Lobby ↔ DHT integration

The lobby screen (`_show_lobby()` in `onboarding.py`) is extended to
perform live DHT operations. The existing Treeview that currently holds
a placeholder row becomes a live table browser.

**Browsing open tables.** Every two seconds the lobby calls
`node.dht_find_providers(LOBBY_TOPIC)` (Phase 3 §3) and, for each
provider found, dials the `/poker/lobby/1.0.0` stream and requests a
signed `table_info` envelope. The Treeview is updated in-place; stale
entries that have not refreshed within 10 s are removed. The entire
walk is non-blocking: it runs in a daemon thread and posts results back
to the Tk event loop via `root.after(0, callback)`.

**Creating and advertising a table.** When the host clicks "Create
Table", the node calls `node.dht_provide(table_topic(join_code))` and
`node.dht_provide(LOBBY_TOPIC)` and begins serving `table_info`
responses on `/poker/lobby/1.0.0`. The join code (encoding the rules
hash) is displayed in the Create Table dialog so the host can share it
out-of-band for private games. The "Create Table" dialog fields map
directly onto `TABLE_RULE` settings; the rules hash is computed via
`settings.rules_hash()` and embedded in the join code.

**Joining a table.** Clicking "Join Table" with a row selected, or
entering a join code directly, triggers
`node.dht_find_providers(table_topic(join_code))`, dials the host
using the Phase 3 connection sequence (direct → DCUtR → circuit relay),
and opens a `/poker/game/1.0.0` stream. A successful connection
transitions out of the onboarding flow and into `Holdem` in multiplayer
mode, with the peer handle passed in.

---

### 4. Game session lifecycle

Once all seats are filled and the host starts the first hand, each hand
follows this sequence:

**`player_info` broadcast.** Every peer sends a `player_info` envelope
(Phase 1 §1 action types) immediately after the DKG handshake completes.
The Tkinter GUI updates the corresponding seat's display name and avatar
from the received payload, using the same `avatar_b64` field written
during onboarding. Peers that cannot decode the avatar fall back to the
colored-circle placeholder used for AI seats.

**DKG handshake.** The per-hand key generation (Phase 1 §3) runs over
the `/poker/game/1.0.0` streams. The GUI shows a "Shuffling…" overlay
and locks the action controls until all `dkg_verify` acknowledgements
are in. The cooperative shuffle (Phase 2 §2) follows immediately;
progress is shown as each seat's `deal_step`/`"shuffle"` envelope
arrives.

**Round loop.** Signed action envelopes from each player arrive over the
game stream, are verified by the Phase 1 codec, and are fed into the
deterministic engine. The local engine drives the GUI exactly as in
single-player mode — the difference is that `hero()` signs and sends the
envelope over the stream before calling `engine.act()`, and actions from
other seats arrive from the stream rather than from `Brain.decide()`.
Phase 2 cooperative partial-decrypt envelopes are interleaved with the
round loop at the dealing moments (hole cards → flop → turn → river →
showdown).

**Reconnect and dropout.** The Phase 1 dropout state machine (§4) runs
on every peer locally. If a peer's action clock expires the remaining
active peers co-sign a `timeout_attest` and the auto-fold fires
automatically. A reconnecting peer sends a signed `session_resume`
envelope and replays the hash-chained log to resync; the GUI returns to
the normal round loop without interrupting play.

---

### 5. PyInstaller packaging

The deliverable is a single-directory PyInstaller bundle targeting
Windows (x86-64), macOS (x86-64 and arm64), and Linux (x86-64). The
spec file bundles everything needed to run without an installed Python:

```python
# holdem.spec (abridged)
a = Analysis(
    ["holdem/__main__.py"],
    hiddenimports=["ristretto255", "cryptography"],
    datas=[("holdem/assets", "holdem/assets")],
)
# Include Go libp2p sidecar binary if py-libp2p fails the Phase 3 gate test.
# go_sidecar_path is set by the build script; omit the entry if None.
if go_sidecar_path:
    a.binaries += [(go_sidecar_path, "go-libp2p-sidecar", "BINARY")]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas,
          name="holdem", console=False, onefile=False)
```

The app detects the sidecar path at runtime via `sys._MEIPASS` when
frozen. A CI step runs PyInstaller in a clean virtual environment,
produces the bundle, and smoke-tests it: the binary must launch,
complete the onboarding flow, deal one hand of single-player, and exit
cleanly — all without a Python interpreter on PATH.


---

### §7 Room Invite Code Format

Room invite codes give two players a shared secret rendezvous point without
any server. The code encodes three fields: a **version byte**, a **peer-id
prefix** (first 8 bytes of the host's libp2p peer ID), and a **rendezvous
key** (8 random bytes used as the DHT topic or relay cookie). A **flags byte**
reserves capacity for future features (e.g. password-protected rooms, spectator
mode, tournament codes).

#### Binary layout (18 bytes total)

| Offset | Length | Field           | Notes                                      |
|--------|--------|-----------------|--------------------------------------------|
| 0      | 1      | `version`       | `0x01` for this format                     |
| 1      | 8      | `peer_id_prefix`| First 8 bytes of host's libp2p peer ID    |
| 9      | 8      | `rendezvous_key`| Random; used as DHT topic / relay cookie   |
| 17     | 1      | `flags`         | Reserved; `0x00` for standard games        |

The 18-byte payload is Base32-encoded (RFC 4648, no padding), producing
29 characters, then grouped into 4-character blocks separated by `-`:

```
XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XX
```

#### Python reference (holdem/p2p/invite.py)

```python
from holdem.p2p.invite import generate_room_code, parse_room_code, format_code, strip_code

# Host creates a code:
code = generate_room_code()          # e.g. "AEYO-UKVC-O6HS-FF7C-T7HT-F7NB-NHBA-A"

# Guest parses it:
info = parse_room_code(code)
# {
#   "version": 1,
#   "peer_id_prefix": "30ea2aa2778f2297",
#   "rendezvous_key": "e29fcf32fda169c2",
#   "flags": 0
# }
```

The host passes `rendezvous_key` as the DHT discovery topic (Phase 3 §3).
Joiners discover the host via `node.dht_find_providers(rendezvous_key)`.
The `peer_id_prefix` lets joiners verify they connected to the right host
before the DKG handshake begins.

The `flags` byte is currently unused but **must be forwarded unchanged**
by all implementations so future flag bits remain round-trippable.
