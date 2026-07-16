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
touching the protocol. Options, from "nothing but us" outward:

- **Direct / LAN** — hand a link to friends. Zero third parties.
- **DHT hole-punching** (Hyperswarm-style): a table is a topic hash
  derived from a join code. The DHT is a public commons, nobody's
  authoritative server, and self-hostable.
- **WebRTC + tracker signaling** (browser-friendly): reaches phones with
  zero install, which is what "online multiplayer" means to most people.
- **Reticulum** — identities are keys, links are end-to-end encrypted,
  transports span TCP down to LoRa radio. A table is just a destination
  hash used as the join code. (Overlaps directly with the mesh-radio
  work already in flight elsewhere.)

---

## Settings and the table contract

Implemented now, in [`holdem/settings.py`](../holdem/settings.py). Every
option has exactly one **scope**, and the scopes are what make the game
multiplayer-ready:

- **CLIENT** — this machine only: theme, animation speed, local
  conveniences, and single-player-only aids. Persisted to a per-user
  config file (which also fixes settings resetting each launch).
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

**Phase 3 — transport.** Reticulum first (it overlaps existing work),
tables as destination hashes, under the current trusted-host assumption
so the crypto can be swapped in behind it. An adversarial-dropper bot —
yanking simulated players at every protocol step and asserting the hand
always terminates with conserved chips — doubles as the integration
fuzzer.

**Phase 4 — clients.** The existing Tkinter app becomes the offline
single-player mode; a browser client (canvas rendering, tracker
signaling) is the zero-install path for everyone else.

The current single-player engine and GUI are not throwaway: the engine
is the authoritative rules core the protocol wraps, and its determinism
is the property the whole design leans on.


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
