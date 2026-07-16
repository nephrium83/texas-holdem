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
