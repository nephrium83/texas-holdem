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
