# L5 scope — wiring the mental-poker crypto into the deal flow

**Status:** planning. **Prereqs:** all complete and CI-green — the crypto
stack (`ristretto`, `elgamal`, `dleq`, `shuffle_mp`, `shuffle_proof`,
`deck_audit`, `keygen_pop`) is built and tested (219 tests). L5 is the
integration layer: turn seven tested modules into a playable,
cheat-resistant deal by rewiring `holdem/p2p/session.py`, and retire the
old commit-reveal `holdem/p2p/shuffle.py`.

This is software engineering, not cryptography. No new primitives.

---

## What exists today (the surface L5 rewrites)

`session.py` (783 lines) already has a **complete async, host-coordinated,
callback-driven shuffle protocol** — but it is the OLD commit-reveal
scheme backed by `shuffle.py`'s `ShuffleRound`, which uses per-recipient
X25519 encryption and a trusted host that sees every hole card (the H-2
trusted-dealer model the whole crypto effort set out to remove).

The existing message flow (dispatch in `handle_message`, lines 113-175):

```
start_shuffle ──shuffle_start──▶ _on_shuffle_start
     │                                  │ (each peer)
     │◀──────shuffle_commit─────────────┘
_on_shuffle_commit
     └─shuffle_commit_collect─▶ _on_shuffle_commit_collect
                                        │
     ◀────────shuffle_reveal────────────┘
_on_shuffle_reveal
_host_finalise_shuffle ──shuffle_deal──▶ _on_shuffle_deal
send_encrypted_hole_cards (X25519 per recipient)
```

Relevant session state (in `__init__`):
- `self._seat_order: list[str]` — conn_ids in seat order (the canonical
  player ordering; already populated and broadcast via `game_start`).
- `self._shuffle_round` — the old `ShuffleRound`; **removed** by L5.
- `self._engine` — the authoritative `holdem.engine.Engine` (host-side).
- `self.is_host`, `self.local_conn_id`, `self._host_conn_id` — the
  host-coordination handles L5 reuses unchanged.

The dispatch table, the transport (`_t.broadcast` / `_t.send`), the
signed-envelope layer (C-1, every message signature-verified in
`wire.unpack`), and the callback pattern (`on_shuffle_ready`, etc.) all
**stay**. L5 swaps the *content* of the shuffle phase, not its shape.

---

## Target protocol (what L5 builds)

Four phases, replacing the single old shuffle phase. All host-coordinated
and callback-driven, matching the existing style. Every message rides the
existing signed envelope.

### Phase A — Key ceremony (DKG)

Establish the joint key `PK = Σ X_i` with proof-of-possession, so no seat
can rogue-key the deck (`keygen_pop`).

```
key_announce {seat, X_i_hex, pop_hex}   broadcast by every seat
```

- Each seat generates `x_i = random_scalar()`, `X_i = x_i·G`, and
  `pop = keygen_pop.prove(x_i, ctx)` where `ctx = session_id | hand_no |
  seat`.
- On receipt, every seat runs `keygen_pop.verify(X_i, pop, ctx)`; a
  failure aborts the hand and attributes it (the announcer is the
  cheat). `keygen_pop.verify_all` is the batch form.
- Once all present seats' shares are verified, each computes
  `PK = elgamal.joint_public_key([X_0..X_{n-1}])` — deterministic, same
  for all.
- New session state: `self._x_share: Scalar` (secret, local only),
  `self._seat_pubkeys: list[Point]`, `self._joint_pk: Point`.

### Phase B — Shuffle chain

Start from the inspection-verifiable trivial deck; each seat shuffles in
turn and proves it.

```
deck_round {round, deck_hex[52][2], shuffle_proof}   broadcast per shuffler
```

- Round 0 is `elgamal.make_trivial_deck()`; every seat checks
  `elgamal.verify_trivial_deck` before accepting. Not transmitted as a
  proof — it's canonical and checked by inspection.
- Seat order defines shuffle order. Seat *s* takes the previous deck,
  runs `deck, wit = shuffle_mp.shuffle_deck(pk, prev)`, then
  `proof = shuffle_proof.prove(pk, prev, deck, wit.perm, wit.scalars,
  ctx=session|hand|round, k=128)`, and broadcasts both.
- Every seat verifies `shuffle_proof.verify(...)` against the *previous*
  accepted deck before accepting the new one. A failed proof aborts +
  attributes (that shuffler cheated) — prevention.
- After the last seat, the final deck is the shuffled encrypted deck.
  New state: `self._deck: list[Ciphertext]` (the current accepted deck),
  `self._shuffle_order: list[int]`, `self._shuffles_done: int`.

**Bandwidth note:** a k=128 proof is ~650 KB; n seats × that per hand is
a few MB. In budget per the broadband-only scope decision.

### Phase C — Deal (selective threshold decryption)

Deal hole/board cards by cooperative partial decryption, so a card is
revealed only to whoever is entitled to it.

- **Position assignment** is public and canonical: given `button` and
  `n` seats, the deal order (hole cards first seat-by-seat, then flop /
  turn / river burns) maps deck positions → destinations exactly as the
  plaintext engine already deals. The map is derived identically by every
  seat, no messages needed.
- **A hole card for seat *t*** at deck position *p*: every seat *s ≠ t*
  sends its partial decryption `D = partial_decrypt(deck[p], x_s)` **with
  a DLEQ proof** to seat *t* only. Seat *t* verifies each DLEQ
  (`dleq.verify(X_s, D, deck[p].c0, proof)`), adds its own share, and
  `combine`s to recover its card. No one else learns it.

```
deal_share {position, seat_from, D_hex, dleq_hex}   sent to the entitled seat
```

- **Board cards** (flop/turn/river) are dealt to *everyone*: all seats
  broadcast their DLEQ-proven shares for the board positions, everyone
  verifies and combines. Public reveal, still cheat-checked.
- Reuses `deck_audit.PositionShare` shape (share + 64-byte DLEQ) — the
  audit and the deal speak the same share format.
- New state: `self._hole: dict[int, str]` (my seat's cards),
  `self._board: list[str]`.

### Phase D — Post-hand audit

At showdown / hand end, every seat opens all 52 positions and everyone
verifies the deck was honest end-to-end (`deck_audit`).

```
audit_open {shares[52]{D_hex, dleq_hex}}   broadcast by every seat
```

- Each seat broadcasts `deck_audit.make_shares(final_deck, x_s)`.
- Everyone runs `deck_audit.audit_deck(final_deck, seat_pubkeys,
  shares_by_seat)`; `ok` must be True. A failure voids the hand and, via
  the chain of accepted `deck_round` decks + `deck_audit.
  first_corrupt_round`, attributes it to the exact shuffler.
- **Accepted consequence:** mucked and burned cards become public here.

---

## Message additions (dispatch table)

New `type` values in `handle_message`, alongside the retained ones:
`key_announce`, `deck_round`, `deal_share`, `audit_open`. The six old
`shuffle_*` types are **removed** with `shuffle.py`.

Every payload is hex-encoded points/scalars/proofs (matching the existing
`*_hex` convention) and rides the signed envelope unchanged.

---

## Session state delta

Remove: `self._shuffle_round`.
Add:
```
self._x_share: Scalar | None            # my secret key share (local only)
self._seat_pubkeys: list[Point]         # X_i for every seat, in seat order
self._joint_pk: Point | None            # PK = sum X_i
self._deck: list[Ciphertext]            # current accepted encrypted deck
self._shuffles_done: int                # how many seats have shuffled
self._hole: dict[int, str]              # my recovered hole cards
self._board: list[str]                  # revealed board
self._audit_shares: dict[int, list]     # collected audit shares by seat
```

`self._x_share` never leaves the process — it is the one piece of
genuinely secret local state, and nothing serializes it.

---

## Build order (each step testable before the next)

1. **`deal_map.py`** — pure function: `(button, n_seats, street) →
   {deck_position: destination}`. Canonical, deterministic, no crypto, no
   network. Mirrors the plaintext engine's deal order exactly. Unit-test
   against the engine's own dealing. *Foundation; unblocks C and D.*
2. **A `MentalDeal` coordinator object** (new module, e.g.
   `holdem/p2p/mental_deal.py`) that owns phases A–D as in-process state
   machines over the crypto stack, transport-agnostic — takes "messages"
   as dicts and emits dicts, exactly like the crypto modules' test
   harnesses. **This is the heart of L5 and where the real testing
   lives:** an n-seat simulation drives a full hand (DKG → shuffle chain
   → deal → audit) with no sockets, asserting the deal is correct and
   every cheat is caught. Built and tested in isolation from `session.py`.
3. **Wire `MentalDeal` into `session.py`** — replace the `shuffle_*`
   handlers and `start_shuffle` with thin adapters that (de)serialize
   dicts to/from transport and drive the coordinator. The coordinator
   holds the logic; the session holds the wiring. Delete `shuffle.py`.
4. **Retire the old path** — remove the six `shuffle_*` dispatch entries,
   `_shuffle_round` state, `send_encrypted_hole_cards` (X25519), and
   `shuffle.py` + its tests. Update MULTIPLAYER.md Phase 2/3 to describe
   the shipped flow.

Steps 1–2 are the bulk and are fully unit-testable headless (the pattern
every crypto layer already used). Step 3 is adapter glue. Step 4 is
deletion + docs.

---

## Open questions to settle before step 2

1. **Dropout mid-hand.** Threshold decryption needs *every* seat's share;
   a seat that disconnects after the shuffle but before the deal stalls
   the hand. The commit-reveal path had `handle_disconnect` /
   `_elect_new_host`. L5 needs a policy: does a mid-hand dropout void the
   hand (simplest, safe, matches the audit's void-on-failure posture), or
   is there a reconstruction path? **Recommend: void the hand on any
   mid-hand dropout for v1** — n-of-n is already required everywhere, so
   this adds no new assumption, and reconstruction is a large separate
   design. Revisit post-v1.
2. **Proof size on the wire.** ~650 KB × n per hand is fine on broadband
   but should be chunked/streamed rather than one giant frame — confirm
   the transport layer's max frame size and whether `deck_round` needs
   fragmentation.
3. **Ordering / turn enforcement.** The shuffle chain is sequential
   (seat *s* can't shuffle until *s-1*'s deck is accepted). The
   coordinator must enforce this and reject out-of-order `deck_round`
   messages — a liveness + soundness concern, cheap to get right.
4. **Where the engine sits — RESOLVED.** The engine was already built
   for this seam: `Engine.start_hand(deck=...)` accepts an injected
   deck, and `Deck.from_indices(shuffled_indices)` exists specifically
   "for the verifiable-shuffle protocol" (per its docstring). So the
   model is NOT "the engine stops dealing" — it is: threshold decryption
   recovers the plaintext card *order*, that order is injected as
   `Deck.from_indices(...)`, and the engine deals from it normally via
   `self.deck.deal()`. Betting, pots, showdown, run-it-twice all stay
   untouched. **Caveat:** this means the full 52-card order must be known
   at `start_hand` time — but mental poker reveals cards *selectively*
   (a hole card only to its owner). Two options: (a) inject only the
   public/eventually-public order and special-case hole cards, or (b)
   keep hole cards hidden in the injected deck and feed each seat its
   decrypted hole cards separately. **This is the one real design
   tension in Phase C and must be settled in step 2** — the engine's
   deck injection assumes a fully-known order, which mental poker
   deliberately does not have until showdown. Likely answer: the
   coordinator drives dealing directly (hole cards via threshold
   decrypt to each owner, board via public threshold decrypt) and only
   uses the engine for betting/pot/showdown logic, calling the engine's
   card-setting paths (`player.hole`, `self.board`) with already-
   decrypted cards rather than injecting a full deck. Confirm which in
   step 2 against the engine's actual `deal`/`next_street` seams.

---

## What L5 does NOT include

- The Godot client (separate track; the client↔engine contract §5 is
  already pinned).
- libp2p transport (Phase 3; L5 is transport-agnostic and tested headless
  over in-process dicts).
- Reconstruction of a dropped seat's share (deferred per open question 1).
- Any change to the crypto primitives — they are frozen and tested.


---

## Decisions settled (2026-07-19)

All ten scoping questions answered. These are build-ready rulings; where
an answer described a real-money or centralized-account feature, it is
recorded as out-of-scope with the reason, since this is a **play-money,
serverless, peer-to-peer** app.

### Architecture

- **Fully hostless (Q7).** No host/coordinator player. All seats are
  equal peers. Every phase (DKG, shuffle chain, deal, audit) is driven
  by canonical rules every peer computes identically from shared public
  state — seat order, whose turn to shuffle, which position deals to
  whom — with no seat holding a privileged coordinating role. This is a
  structural change from today's host-coordinated `session.py`; the
  `MentalDeal` coordinator is written peer-symmetric from the start.
  *Consequence:* protocol steps that the old code funnelled through the
  host (collect commits, finalise) become "every peer broadcasts, every
  peer tallies against the same rule." Turn-taking (shuffle order) is
  enforced by the canonical seat order, not by a coordinator granting
  turns.

- **Transport stays decoupled (Q8).** L5 is built and tested entirely
  headless over in-process/loopback message passing (dicts in, dicts
  out), exactly like the crypto modules' test harnesses. Real networking
  (join-code → connection) is a separate layer that plugs in later;
  nothing in L5 assumes sockets. Multi-instance realism (copies on
  Unraid + laptop simulating internet peers) is a later integration-test
  step, not part of L5.

- **Headless, no UI (Q10).** L5 ships as tested logic with no screen.
  The Tkinter window is not kept in sync during L5; the visible client
  (Tkinter harness and/or Godot) is wired up entirely afterward against
  the finished coordinator.

### Cheating response (Q6) — non-financial only

Real-money mechanisms from the Q6 answer (slashing, escrow, buy-in
freeze/seize, victim compensation, wallet/smart-contract blacklisting)
are **out of scope**: there is no money, custody, wallet, or escrow in
this app, and adding them would make it a regulated money-transmitting
service — a different project. Global identity bans (email / MAC /
hardware UUID / IP blacklist) are **out of scope** too: there is no
central account authority to maintain such a list, and harvesting
hardware identifiers is a privacy line we won't cross.

What L5 **does** build, on a cheat caught by proof/audit failure:
- **Void the hand** — no result stands; chips as they were at hand start.
- **Public attribution broadcast** — a system event every peer displays:
  "Player X removed — verification failure." Proves the security is
  working (builds trust for honest players).
- **Eviction** — the cheating seat is dropped from the table; its seat
  is marked empty; the button advances and play continues with the rest.
- **Local refuse-list** — each peer records the offending **public key**
  and declines to seat it again. This is the decentralized, privacy-
  respecting analogue of a ban: local, per-peer, keyed on the crypto
  identity that actually misbehaved, with no central authority and no
  hardware fingerprinting.

### Mid-hand dropout (Q1) — void for v1

Any seat disconnecting mid-hand **voids the hand** (chips returned,
redeal with remaining seats). Because full decryption already requires
every seat (n-of-n), a mid-hand dropout stalls the hand regardless, and
voiding matches the audit's void-on-failure posture — no new trust
assumption.

**Deferred to v2 (recorded, not built):** Verifiable Secret Sharing /
threshold key reconstruction so remaining seats can pool shares to
reconstruct a dropped seat's key and finish the hand (fold/all-in the
absentee per room rules). This is genuine mental-poker cryptography and
arguably larger than all of L5; it directly conflicts with the
detection-first / get-it-playable priority, so it is a post-v1 upgrade.

### Crash survival (Q9) — deterministic keys + persisted public state

A hand **survives the app closing/reopening** within a grace window.
Design:
- **Deterministic hand keys.** A seat's secret key share for a hand is
  derived, not stored volatile: `x_share = HKDF(local_master_secret,
  session_id ‖ hand_id ‖ seat)`. On reopen the app recomputes the exact
  same share instead of losing it. The **master secret is a local
  device secret** generated once and saved to the app data dir — *not* a
  crypto wallet, seed phrase, or HD-wallet key (that framing came from a
  money-app source and does not apply here).
- **Persisted public state.** Public game state (pot, bets, board,
  publicly-decrypted cards, current `hand_id`, accepted deck rounds) is
  written to a local store (SQLite) as it changes.
- **Re-sync on reopen.** Fetch current state from peers → recompute hand
  keys deterministically → replay betting history to catch up → resume.
- **Grace window.** A closed/crashed app is treated as a normal
  disconnect: the hand stays alive for **15–20 s** while the seat's
  action clock ticks; reopen-and-handshake within the window resumes,
  otherwise the dropout rule (void, per Q1) fires.

This also affects the DKG: because key shares are deterministic from the
master secret + hand id, `keygen_pop` proves possession of a
deterministically-derived share, and a rejoining seat regenerates the
identical share — key derivation must be settled in the coordinator's
Phase A design (step 2).

### Table size & latency (Q4/Q5) — measured, not estimated

Cap tables at **9 seats**. Budget: **≤ 60–90 s per hand**. Measured on
the dev machine (single-threaded libsodium), per shuffle proof at k=128:
**~1.1 s to prove, ~1.1 s to verify, 637 KB (≈1.3 MB hex on the wire).**

- **Prevention ON at 9 seats:** the shuffle chain is serial (seat *s*
  can't shuffle until *s-1*'s deck is verified), so ~9×(prove+verify) ≈
  **~20 s of compute** plus ~10 MB of proof traffic — lands inside 90 s
  but is heavy, and dominated entirely by the shuffle proofs. DKG +
  audit are negligible (~0.2 s combined for 9 seats).
- **Detection ONLY at 9 seats:** no shuffle proofs sent; cost is just
  the post-hand audit, measured at **~0.18 s total for 9 seats** — well
  under a second, feels instant.

**Ruling (with Q3): detection-only is the v1 default.** It is ~100×
cheaper at the table and trivially inside budget at a full 9-max. The
prevention layer (`shuffle_proof`, already built and tested) is retained
as an **opt-in table setting**, with a UI warning that enabling it at
large tables adds ~20 s/hand. Likely future policy: prevention for
small/higher-stakes tables, detection for casual 9-max.

### Confirmed without change

- **Q2** — folded/burn cards going public at hand-end audit: accepted.
- **Q3** — detection-first: adopted as the default mode (see Q4/Q5). This
  overrides the prevention-heavy parts of the Q1 and Q6 answers.

### Net effect on the build order

The four-step build order above is unchanged, but two items move earlier
because the settled decisions make them foundational rather than
optional:
1. **Deterministic key derivation** (`HKDF(master_secret, session ‖ hand
   ‖ seat)`) is part of the coordinator's Phase A from the start, not a
   later crash-recovery bolt-on — it changes how shares and their PoPs
   are generated.
2. **Peer-symmetric (hostless) state machine** is the coordinator's
   basic shape from line one, not a refactor of a host-centric design.

Deferred cleanly to post-v1: VSS dropout continuance, the prevention
layer as default, and any persistence beyond what crash-survival needs.


---

## Phase C — engine seam confirmed (2026-07-19)

Read the actual engine code (`Deck`, `Deck.from_indices`, `start_hand`,
the hole-deal loop, `next_street`) and verified the integration seam
empirically. The Phase-C design tension is resolved, and one real gotcha
surfaced that the earlier note missed.

### How the engine deals (ground truth)

- **Hole cards** (`start_hand`, ~line 575): builds a seat `order` starting
  one seat left of the button, then deals **two rounds of one card each**,
  seat by seat, `self.deck.deal(1)` per card. That is the real alternating
  deal — NOT "seat 0 gets both its cards, then seat 1." The deal-position
  sequence for h holes over s seats is: round-1 to each seat in order,
  then round-2 to each seat in order.
- **Board** (`next_street`): flop `deal(3)`, turn `deal(1)`, river
  `deal(1)`. **No burn cards** — the engine does not burn before streets.
  (Real casinos burn; this engine does not, so `deal_map` must match the
  engine, not casino convention. Burn cards, if ever added, are an engine
  change first.)
- `Deck.deal(n)` pops from the **end** of `self.cards`, and
  `Deck.from_indices(indices)` stores `reversed(indices)` so that
  `indices[0]` is served first. So an injected order is consumed
  front-to-back as `indices[0], indices[1], ...`.

The canonical deal-position order is therefore fully determined by
`(button, seats_in, holes=2)` and is identical for every peer — exactly
what `deal_map.py` must produce, and it must mirror THIS loop (left of
button, round-robin, two passes, then flop/turn/river with no burns).

### The gotcha: two different card orderings

The engine's `FULL_DECK` and mental-poker's `elgamal.CARDS` are **not the
same order**, so `Deck.from_indices` cannot be fed mental-poker indices
directly:

- `FULL_DECK` = `Card(v, s) for v in 2..14 for s in 0..3` — **rank-major**
  (2c,2d,2h,2s, 3c,...). Index = `(v-2)*4 + s`.
- `elgamal.CARDS` = `r+s for s in "cdhs" for r in "23456789TJQKA"` —
  **suit-major** (2c,3c,...,Ac, 2d,...). Index = `suit*13 + rank`.

They agree at index 0 (2c) and 51 (As) by coincidence but diverge
everywhere between. A translation function is required and was verified
to be an exact bijection where every card matches by (rank, suit):

```
elgamal_index -> fulldeck_index:
    rank, suit = card[0], card[1]
    v = "23456789TJQKA".index(rank) + 2
    s = "cdhs".index(suit)
    return (v - 2) * 4 + s        # == FULL_DECK position
```

This translation is a small pure helper that belongs in `deal_map.py`
(or a shared card-index module), with a round-trip test pinning it. It is
the exact seam where a silent off-by-ordering bug would deal wrong cards,
so it is tested against `FULL_DECK`/`elgamal.CARDS` directly.

### Resolved integration model for Phase C

The coordinator does NOT inject a full 52-card `Deck` (mental poker never
knows the full order until showdown — the original tension). Instead:

1. **`deal_map(button, seats_in)`** yields the canonical mapping of deck
   position -> destination (which seat's hole, or which board slot),
   mirroring the engine's deal loop above. Pure, no crypto, deterministic.
2. **Hole cards:** for each hole position, the entitled seat gathers the
   other seats' DLEQ-proven partial decrypts, combines, recovers its card
   (as an `elgamal` card label), and **sets it on the engine directly**
   via the player's `hole` list — translating the label to a `Card`.
3. **Board cards:** at each street the board positions are decrypted by
   everyone (public threshold decrypt), and the coordinator appends the
   resulting `Card`s to `self.board` / feeds them so `next_street`'s
   state advances consistently.
4. The engine runs **betting, pots, showdown, run-it-twice** unchanged;
   it simply receives already-decrypted cards instead of dealing them
   from `self.deck`.

So the engine is used for everything EXCEPT card generation, via its
existing card-holding fields (`player.hole`, `self.board`) — confirmed
present and populated exactly this way by `start_hand`/`next_street`.
`Deck.from_indices` stays available but is **not** the Phase-C path
(it would require revealing the whole order); it remains useful only if a
future variant wants a fully-public deck.

**Action for step 2:** `deal_map.py` includes (a) the position->destination
map mirroring the engine loop, and (b) the elgamal<->FULL_DECK index
translation, each with tests against the engine's real ordering. The
coordinator's Phase C sets cards on `player.hole` / `self.board` rather
than injecting a `Deck`. `next_street` interaction (advancing street state
while supplying board cards) is the one spot to integration-test carefully,
since the engine normally couples "advance street" with "deal board" in the
same call — the coordinator must supply the board cards in a way consistent
with that coupling (likely: pre-load the engine's deck with just the board
cards it will deal for that street, or set `self.board` and advance state
directly — decide against the running engine in step 2).

---

## Deferred to v2 (the backlog)

Single collected list of everything consciously pushed past v1, so none
of it is lost in the inline prose above. Each item is *recorded, not
built*; v1 ships without it.

### The v2 gate (hard stop — set 2026-07-21)

**No v2 item below is started before ALL of the following are true.**
This is a hard boundary, not a guideline: v2 priorities must come from
real play, and real play requires the whole v1 path to exist first.

1. ~~Legacy commit-reveal shuffle deleted~~ — **DONE 2026-07-21**
   (L5 step 4).
2. **Continuous play** — multi-hand sessions with stack carry,
   dead-button rotation, eliminations, and heads-up, tested on the bus.
3. **Godot client MVP** — a full session playable against the local
   sidecar through the client server (connect → hands → settle → next).
4. **Dropout timeout → void** — a silent peer voids the hand instead of
   hanging it. The void path exists; the timer trigger is pulled *into*
   v1 — it is not v2 work.
5. **Real-transport playtest** — a join-code hand between at least two
   physical machines. (Backlog items 3–4 below graduate to the v1.x
   ship path to make this possible; crossing this line opens the gate.)

When the gate opens, v2 order comes from observed pain at real tables —
void frequency drives #1, cheat pressure and table type drive #2, wire
behaviour drives #5, player demand drives #7 — not from the numbering
here.

### Cryptographic / protocol

1. **VSS dropout continuance.** Verifiable Secret Sharing / threshold key
   reconstruction so remaining seats can pool shares to reconstruct a
   dropped seat's key and finish the hand (fold/all-in the absentee per
   room rules) instead of voiding. Genuine mental-poker crypto, arguably
   larger than all of L5. *v1 instead:* void the hand on any mid-hand
   dropout (Q1).

2. **Prevention layer as default.** The shadow-deck shuffle proof
   (`shuffle_proof`, already built and tested) runs as an **opt-in table
   setting** in v1, not always-on, because at a 9-max table it costs
   ~20 s and ~10 MB per hand. *v2 direction:* smarter policy — e.g.
   prevention auto-enabled for small or higher-stakes tables, detection
   for casual 9-max — plus proof-size/perf work (parallelism, smaller k
   where justified, streaming) to make always-on cheaper.

### Networking / transport

3. **Real network transport.** Join-code → connection (libp2p per
   Phase 3, DHT/relay/NAT traversal). L5 is transport-decoupled and
   tested headless; the real transport plugs in afterward.
   *Reclassified 2026-07-21: this is v1.x ship-path work, not v2 polish —
   the product does not ship without it. It is gate criterion 5, built
   after the Godot MVP.*

4. **Multi-instance integration realism.** Running copies on Unraid +
   laptop to simulate internet peers end-to-end. A later integration-test
   milestone, after the headless coordinator and the transport both
   exist. *Reclassified 2026-07-21: lands with #3 as part of closing the
   gate (v1.x).*

5. **Proof fragmentation on the wire.** ~1.3 MB (hex) proofs should be
   chunked/streamed rather than sent as one frame; depends on the real
   transport's max-frame behaviour, so it lands with #3.

### Client / UI

6. **Visible client wired to the new deal.** The Tkinter harness and/or
   Godot client are connected to the finished coordinator *after* L5.
   L5 itself is headless (Q10). The client↔engine contract (§5) is
   already pinned, so this is wiring, not design.
   *Status 2026-07-21: in progress — the sidecar half is DONE
   (`holdem/client_server.py`, localhost TCP + newline-JSON per
   GODOT_PROTOCOL.md, tested over a real socket). The Tkinter half is
   RETIRED, not migrated. The Godot half is the current focus and is
   gate criterion 3.*

### Persistence (beyond crash-survival)

7. **Richer persistence.** v1 persists only what crash-survival needs
   (public game state + `hand_id` in SQLite, deterministic key
   re-derivation). Hand-history archives, cross-session stats tied to the
   P2P deal, replay storage, etc. are later.

### Enforcement (explicitly NOT v2 either — permanently out for this app)

Recorded here so they are never mistaken for backlog: real-money
mechanisms (slashing, escrow, buy-in seize, victim compensation, wallet /
smart-contract blacklisting) and global identity bans (email / MAC /
hardware UUID / IP). These are out because the app is play-money and
serverless — adding money custody would make it a regulated money
transmitter, and there is no central authority to hold a global banlist.
They are **not** deferred features; they are out of scope by design.
Local, per-peer pubkey refuse-lists are the decentralized substitute and
ARE in v1.
