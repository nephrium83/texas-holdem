# Suspension, Reconnect and Crash Recovery — Research

**Type:** Research / evidence. **Not** a normative protocol requirement.
**Date:** 2026-08-18
**Base:** `07f61a7` (origin/main)
**Status:** **INCOMPLETE.** The recovery-mechanism threat analysis
(durable WAL vs transcript replay vs peer replay) did not run — three
agents were lost to a usage limit. Sections below are what was
established; the mechanism comparison is outstanding.

## Headline

Reconnect is **not supported today**, and the blocker is not the one an
earlier reading suggested.

The *authorization* layer is already connection-independent and ready:
`_author_owns_seat` (session.py:1620-1656) authorizes on the Ed25519 key
bound to the seat, and `_bind_seat_keys` (session.py:1570-1594) freezes
that binding once, one-way, before the first hand. `handle_disconnect`
(session.py:2677-2717) never clears `_seat_keys` or `_seat_order`. A
returning peer's messages would already be authorized.

**But the cryptographic domain is derived from `conn_id`**, and that is
fatal to naive reconnect.

## B7 — `conn_id` is baked into the crypto domain

`_deal_context_bytes` (session.py:855-863) hashes the seat-order
**`conn_id` strings**. That digest becomes `_deal_session_id()`
(session.py:889-891), which is the HKDF `session_id` in `derive_share`
(mental_deal.py:155-173) *and* the PoP context (mental_deal.py:174-179)
*and* the Bayer–Groth statement context (mental_deal.py:507-509).

A `conn_id` is a per-socket UUID4 with no cryptographic content and no
persistence — `_new_conn_id()` is `str(uuid.uuid4())`
(transport.py:435-436), minted fresh for every accepted socket, every
direct connect and every relay connect.

So a peer that reconnects gets a new `conn_id` → a different
`session_id` → a **different `x_share`** → it announces a different `X`
for the same seat → peers that already accepted the first announcement
abort:

```
if seat in self._pubkeys and bytes(self._pubkeys[seat]) != bytes(X):
    return self._abort(f"seat {seat} announced conflicting key shares", seat)
```
(mental_deal.py:438-439)

**The honest returning peer is blamed for the abort.** Any reconnect
design must first sever `conn_id` from the deal context — replacing it
with the stable per-seat Ed25519 key is the obvious candidate, and is a
wire-breaking change to the deal domain.

## B8 — Envelopes expire; stored transcripts cannot be replayed

`wire.unpack` enforces a hard ±30 s freshness window:

```
skew_ms = abs(time.time() * 1000 - msg["ts"])
if skew_ms > 30_000: raise ValueError(...)
```
(wire.py:190-195)

Every envelope from earlier in the hand is already expired. Replaying
stored envelopes is therefore impossible **without re-signing**, and
re-signing destroys the property that made the transcript trustworthy.

## B9 — Honest re-sends are indistinguishable from equivocation

`wire.pack` stamps a fresh `ts` into the signed pre-image and into the
hash (wire.py:138-152), so the same logical message re-sent produces a
**different envelope hash**. `_author_seq_ok` (session.py:1175-1202)
binds each `author_seq` to a fingerprint and treats a second, different
fingerprint at the same number as equivocation — which voids the hand,
**blaming the honest returning seat**.

This is live today and needs no attacker: it is what happens on any
honest re-send.

Compounding it, `_author_seq_out` for the local seat
(session.py:543, 1137-1139) is *locally generated and never broadcast* —
no peer holds it. Restarting it at `AUTHOR_SEQ_START` re-issues numbers
peers have already bound to different fingerprints, producing exactly
the same self-inflicted equivocation void.

## Nothing is persisted

There is **no message log of any kind** for the current hand. Every
container was enumerated:

- `_msg_buffer` holds only **future**-hand messages (session.py:1672-1677); current-hand messages are never added.
- `_deal_outbox` is outbound-only and drained to empty in place.
- `_author_seq_seen` stores `{author_seq: fingerprint}` — the *identity* of a message, never the message.
- Applied `bet_action`s are consumed by `apply_action` and discarded. `ReplicaTable` keeps no applied-action log.
- Deal messages are handed to the driver and discarded by the Session.

The only disk writes in the P2P layer are `identity.py` and
`device_secret.py`. There is no `snapshot`/`restore`/`__getstate__`
anywhere under `holdem/`.

### What `device_secret.py` already solves (and why it exists)

Its docstring names the exact historical bug this workstream is
circling: the session once generated a fresh `os.urandom(32)` per
process, "so a peer that crashed and rejoined derived a DIFFERENT public
share for the same seat, and the peers that had already accepted the
first one aborted the hand." It is atomic and refuses silent
regeneration. Half of failure-class 2 is therefore already solved
deliberately — the master secret survives a crash. The rest of the
state does not.

## Resume-state classification (crypto layer)

| Field | Class | Note |
|---|---|---|
| `session_id`, `hand_no`, `seat`, `seats_in`, `button` | derivable | but see **B7** — `session_id` currently depends on `conn_id` |
| `master_secret` | **must persist** | already persisted (`device_secret.py`) |
| `_x_share` | derivable | from the tuple above; nothing else |
| `prevention` | **must persist** | see below |
| `phase`, `_deck`, `_shuffle_round`, `_shares`, `_hole`, `_board`, `_revealed_streets`, `_audit_shares` | transcript-reconstructable | but the transcript is neither retained (above) nor replayable (**B8**) |
| `_round_decks` | memory only | destroyed on crash; needed for later chain attribution |
| local seat's recovered hole cards | locally generated | never broadcast; no peer can return them |
| `_author_seq_out` | locally generated | see **B9** |

### `prevention` must persist — silent default is the downgrade

`MentalDeal.prevention` defaults to `False` (mental_deal.py:201) and is
**not negotiated in the deal transcript**. A rebuilt instance that
defaults it to `False` stops verifying and, if it shuffles again, omits
its proof — which prevention-mode peers reject, aborting and blaming the
honest reconnecting seat. Silently defaulting is precisely the downgrade
the Bayer–Groth mandate exists to prevent.

### `button` disagreement leaks cards

`_enter_deal` broadcasts this seat's decryption share for every hole
position it believes it does *not* own (mental_deal.py:687-691). Under a
wrong `button`, that set can include its **own** hole positions —
directly leaking its cards. The privacy argument at mental_deal.py:666-673
depends entirely on button agreement.

## Reusable machinery (the good news)

- Stable persisted Ed25519 identity across restarts (`identity.py`), explicitly documented as being for reconnecting players.
- Stable persisted device secret → identical share on return (`device_secret.py`).
- Immutable one-way seat→key binding that survives disconnect untouched.
- Author-vs-connection separation, already built and documented as relay-motivated.
- A full mutual-authentication handshake with server-chosen freshness, one-use connection-scoped challenges, 30 s TTL, and grinding resistance (`admission.py`).
- Admission state is deliberately torn down on disconnect, so a reconnect must re-authenticate from scratch against a fresh nonce.
- Per-author replay/equivocation detection keyed `(hand, seat)`.

## Additional blockers found

- **A returning peer cannot compute its own seat.** `begin_hand` raises unless `self.local_conn_id in order`, and `local_seat` is `self._seat_order.index(self.local_conn_id)` (session.py:1069-1074, 1828).
- **`local_conn_id` can only be relearned from `player_ack`, which is LOBBY-only** (session.py:2477-2480). Mid-hand the session is `PLAYING`, so a returning peer has no shipped way to be told its identity again.
- **Roster state for the missing peer is destroyed on disconnect** — `players.pop`, `_join_order.remove` (session.py:2698-2701). Nothing retains "seat 3 belongs to key K, currently absent."
- **Host loss during play is terminal by design** and explicitly refuses migration in wire mode (session.py:2704-2709, 2755-2764). In the production star topology only the host listens.
- **Admission authorizes "holds the invitation", never "is entitled to seat N".** `admission.py:38-44` says so outright and disclaims Sybil resistance. Seat entitlement at reconnect would be new work.

## Defects in `main` found incidentally

These are **not** hypothetical reconnect issues; they exist now.

| ID | Defect | Evidence |
|---|---|---|
| **D1** | `_bind_seat_keys` accepts a **partial** map (`if bound: self._seat_keys = bound`, session.py:1593-1594) and is one-way. A peer that disconnects in the `start_game` → `start_p2p_hand` window is popped from `players` first, so its seat freezes with **no key** and is permanently unauthorizable for the session. | session.py:1587-1594, 2698-2699, 1855 |
| **D2** | `_on_player_info` has no lifecycle gate — only `if not self.is_host` (session.py:2289-2293), unlike `_on_player_ack` and `_on_game_start`. Any holder of the room code can admit a fresh identity mid-hand, land in `self.players`, and trigger a roster broadcast. It gains no seat (bindings are frozen) but mutates lobby state during play. | session.py:2289-2312 |
| **D3** | `MentalDealDriver.all_hole_cards()` has **no phase gate** (mental_deal_driver.py:145-153) — it returns every seat's hole cards whenever the audit passed, including on fold-wins where the stock UI hides them (`client_view.py:131-134` is gated on `result["runs"]`, which `engine.py:910` leaves empty when one player is live). A modified client harvests opponents' holes on every hand. | mental_deal_driver.py:145-153 |
| **D4** | Detection-only is reachable on compat tables. `parse_deal_policy` and `_assert_deal_preconditions` enforce Bayer–Groth **only** when `author_mode == AUTHOR_MODE_WIRE` (session.py:359, 1044). On a compat multi-peer table a shuffler may broadcast 52 arbitrary fresh encryptions with no proof; `_on_deck_round`'s trivial-ciphertext scan is no obstacle because fresh encryptions have random `c0`. | session.py:359, 1044; mental_deal.py:614-627 |

## Sole-live-player exception — REFUTED as stated

The proposal (award the pot to the sole live seat without board recovery
or audit) was examined and **both adversarial reviewers refuted it with
high confidence**.

Initial verdict was `CONDITIONALLY_SAFE`; it does not survive.

**Refutation 1 — the claim is unqualified but only holds under
prevention mode**, and the mode gate is transport-conditional (**D4**).
On a detection-only table the audit is the *only* deck check that
exists, and the exception destroys it permanently — the vanished seat's
HKDF-derived share is unreproducible and `_round_decks` is memory-only.

**Refutation 2 — the audit is the only point in the entire protocol
where a seat publishes a DLEQ-proven share for its OWN hole cards.**
`_enter_deal` records the owner's own-hole share locally and explicitly
does not send it (`if owner == self.seat: continue`,
mental_deal.py:687-691). `open_audit` calls
`deck_audit.make_shares(self._deck, self._x_share)` over the *whole*
deck with no owner exclusion. Skipping the audit on a fold-win therefore
removes the only reciprocity: today a winner must publish its own holes
to get paid; under the exception it need not — while (via **D3**) still
harvesting everyone else's on audited hands.

Additional exploits the proof itself identified:

- **The beneficiary could be the disappeared peer.** Nothing in `engine.settle()` checks connectivity, and nothing outside the betting timeout clears `in_seat` or sets `folded`, so a peer that disconnects after the last fold stays live and would be paid.
- **Timeout-manufactured folds must not qualify**, or a peer able to stall two opponents wins the pot for the cost of the blinds — with its own uncalled bet refunded (`engine.py:864-872`).

**Conclusion: do not implement the sole-live-player exception.** If it is
revisited, it requires at minimum mandatory prevention mode enforced
independently of transport, proof that the settling replica itself
verified every shuffle round (`_proofs_verified == len(seats_in)`),
exclusion of the disappeared peer as beneficiary, exclusion of
timeout-manufactured folds, and retention of `_round_decks` plus received
audit shares so the hand can be audited later rather than having its
evidence destroyed.

## Outstanding

- Recovery-mechanism threat comparison (durable WAL / transcript replay / peer replay) — **not run**.
- Prior-art synthesis was collected but is not yet distilled into a recommendation.
- Whether the relay path preserves or reassigns `conn_id` on reconnect through the same room.
- Whether `Player.seat_index` (session.py:122) is dead — declared, no assignment found in `session.py`.
