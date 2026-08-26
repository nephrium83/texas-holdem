# M2 Recovery-Mechanism Threat Analysis

**Type:** Research / evidence. **Not** a normative protocol requirement.
**Date:** 2026-08-26
**Base:** `4b5e85a` (`main`, post-M0, post-M1)
**Supersedes the "Outstanding" section of** `docs/research/p2-suspension-reconnect.md`
(2026-08-18, base `07f61a7`), which recorded the mechanism comparison as
**not run**.
**Normative output:** `docs/RECOVERY_SPEC.md`. Nothing in this note binds
anything. Where this note and that spec disagree, the spec wins for
implementation and this note wins for *why*.
**Revised 2026-08-26** after independent review of the first increment, which
rejected three conclusions: that host process restart was out of reach (§7),
that a local grace timer could drive a terminal transition (§8), and that
`HAND_OPEN` needed no canonical encoding (spec §6.8). The superseded
reasoning is restated in each section rather than deleted — the arguments were
plausible, and a later reader who reconstructs them deserves to find out here
why they fail.

This workspace covers M2 — suspension, reconnect, and crash recovery — as
defined by `docs/ROADMAP.md`. Candidate designs recorded here do not become a
protocol contract silently.

Method: reading the merged baseline. **No repository code was executed on the
host that produced this note**, so every claim below is a citation to source,
never a test result. Claims that need execution are named as such in §9.

---

## 1. Revalidation of the 2026-08-18 research against the merged baseline

The prior research note was written against `07f61a7`, before M0 (PR #39) and
M1 (PR #40) merged. Every load-bearing claim was re-checked against `4b5e85a`
before anything was promoted. Line numbers below are this baseline's.

### 1.1 Still true, unchanged

| Claim | Status at `4b5e85a` | Evidence |
|---|---|---|
| **B7** — `conn_id` is hashed into the deal domain | **live** | `session.py:897-903` iterates `self._seat_order` (conn_id strings) into the pre-image; `session.py:906-933` digests it; `session.py:1137` feeds it to the driver |
| A reconnecting peer derives a different `x_share` | **live** | `mental_deal.py:165` — HKDF info is `poker.share.v1\|{session_id}\|{hand}\|{seat}` |
| …and is blamed for the abort | **live** | `mental_deal.py:438-439`, unchanged text |
| **B8** — hard ±30 s envelope freshness | **live** | `wire.py:190-195` |
| **B9** — `pack` stamps a fresh `ts`, so a re-send hashes differently | **live** | `wire.py:143` (`"ts": int(time.time() * 1000)`), covered by the signature at `wire.py:147` and the hash at `wire.py:150-151` |
| …and `_author_seq_ok` reads that as equivocation and voids | **live** | `session.py:1234-1255` |
| **B2** — nothing persists hand or session state | **live** | the only disk writers under `holdem/p2p/` remain `identity.py` and `device_secret.py`; there is no `snapshot`/`restore`/`__getstate__` |
| `_author_seq_out` is locally generated and never broadcast | **live** | `session.py:1190-1192`; no peer holds it |
| `prevention` defaults to `False` and is not in the deal transcript | **live** | `mental_deal.py:201` |
| A wrong `button` makes a seat broadcast shares for its *own* holes | **live** | `mental_deal.py:676-692` — the `if owner == self.seat: continue` exclusion is computed from `self.button` |
| A returning peer cannot compute its own seat | **live** | `session.py:1123-1127`, `session.py:1943-1944` — both index `_seat_order` by `local_conn_id` |
| `local_conn_id` is relearnable only from `player_ack` | **live, and now strictly narrower** | `session.py:2606-2609` — M0 added a LOBBY-only gate, so mid-hand relearning is not merely unshipped, it is refused |
| Roster state for a dropped peer is destroyed | **live** | `session.py:2847-2850` — `players.pop`, `_join_order.remove` |
| Seat authority survives disconnect | **live** | `handle_disconnect` (`session.py:2824-2866`) touches neither `_seat_keys` nor `_seat_order` |
| Host loss during PLAYING is terminal, and wire mode refuses migration | **live** | `session.py:2852-2858`, `session.py:2903-2911` |
| Admission proves "holds the invitation", never "is entitled to seat N" | **live** | `admission.py:38-44` |
| Admission state dies with the connection | **live** | `session.py:2841-2842` — `forget(conn_id)` runs even on a terminal session |
| Sole-live-player settlement exception is REFUTED | **unchanged** | the audit remains the only point where a seat publishes a proven share for its own holes (`mental_deal.py:680-683` records and does not send; `deck_audit.make_shares` at `deck_audit.py:55-69` covers the whole deck with no owner exclusion) |

### 1.2 Changed by M0 — the four defects

The prior note's defect table describes `07f61a7`. On `4b5e85a`:

| ID | Was | Now |
|---|---|---|
| **D1** | `_bind_seat_keys` froze a partial map | **fixed.** All-or-nothing: `session.py:1702-1709` raises on an incomplete map, and `session.py:1692-1700` raises on an empty one in wire mode. The map is still idempotent and one-way (`session.py:1674-1675`) |
| **D2** | `_on_player_info` had no lifecycle gate | **fixed.** `session.py:2415-2420` refuses outside LOBBY |
| **D3** | `all_hole_cards()` had no phase gate | **closed as not a defect at the named site** (ROADMAP, M0). `mental_deal_driver.py:153-163` still gates only on `audit_report.ok`; the reveal is a deliberate product decision recorded in `POKER_RULES_PROFILE.md` §4, D-M1-1 |
| **D4** | prevention keyed to `author_mode` | **fixed.** `parse_deal_policy` (`session.py:369-401`) refuses detection-only in wire mode, and `_assert_deal_preconditions` (`session.py:1091-1101`) keys enforcement to the **adopted policy** first and `author_mode` second |

**Consequence for M2, and it is not cosmetic.** The refutation of the
sole-live-player exception rested partly on D4 — "the mode gate is
transport-conditional". That leg is gone. The refutation nevertheless
**stands**, on its second and independent leg: the audit is the only
reciprocity point in the protocol, and skipping it lets a fold-winner be paid
without ever publishing a proven share for its own holes. M2 does not revive
the exception, and the ROADMAP's "Do not implement it" is unaffected. Recording
this because a later reader who checks only the first argument will find it no
longer applies.

### 1.3 Changed by M1

M1 froze `poker.tda.2024.nlhe.v1` and its canonical encoding
(`POKER_RULES_PROFILE.md` §1.1-1.3) and deliberately did **not** bind it
(§1.4, and ROADMAP Decisions 2026-08-18). The binding is M2's, in the same
break as B7. Nothing in M1 touched `_deal_context_bytes`; verified by reading
it — `session.py:891-904` still encodes exactly (label, version, policy, seat
order).

### 1.4 New findings, not in the prior note

**F-1 — `identity.py` silently regenerates the protocol identity on a
corrupt file, and its write is not atomic.**

`identity.py:45-49` wraps the read and parse in `try: … except Exception:
data = {}`. Falling through with an empty dict takes the generate branch
(`identity.py:58-61`), and `identity.py:73-74` then does
`path.write_text(json.dumps(data))` — **overwriting** the file. So a
partially-written or corrupt `identity.json` mints a brand-new Ed25519
identity and destroys the old one, with no error and no log line.

`write_text` is not atomic. A crash or a full disk mid-write leaves exactly
the truncated file that the *next* launch swallows and replaces. That is a
self-amplifying failure: one torn write becomes permanent identity loss on the
following start.

Why this matters for M2 specifically: the seat↔key binding is the entire basis
of exact-seat reconnect. Silent identity regeneration converts **failure class
2** (process restart on the same device, recoverable) into **failure class 3**
(secret loss, unrecoverable) *without anyone observing the transition*. The
returning peer looks healthy, completes admission under a new key, and is then
refused for its seat by `_author_owns_seat` (`session.py:1761-1764`) with no
indication that the cause was a corrupt file rather than an impostor.

This is precisely the failure `device_secret.py` exists to prevent, and its
module docstring cites `identity.py`'s stability as the precedent it was
matching. `device_secret.py` fails loudly on a wrong-size file
(`device_secret.py:146-152`) and publishes via an atomic link
(`device_secret.py:96-100`). `identity.py` does neither. The hardening is
asymmetric between the two files that hold the two secrets a seat cannot be
recovered without.

Partially mitigating, and stated for accuracy: not every corruption is silent.
`identity.py:55` and `:65` call `base64.b64decode` **outside** any `try`, so a
file that parses as JSON but holds malformed base64 raises at module import.
The silent path is specifically "not valid JSON" — which is exactly what a torn
write produces.

**F-2 — `DeadlineToken.actor` is a `conn_id`.**

`timeout.py:65-68` types `actor` as the awaited peer's `conn_id`, and
`hand_id` as `_deal_session_id()`. B7 named the deal domain; this is a second,
smaller `conn_id` dependency in the timeout path. It is not cryptographic — a
mismatched token makes a proposal stale rather than forging one — but a
reconnecting peer changes `conn_id`, so every in-flight token naming it stops
matching. M2 must not leave the timeout path referring to a dead hop. The fix
is the same stable seat identity; the *semantics* belong to M3/M4.

**F-3 — a void reverts stacks, so an equivocator recovers its committed
chips.**

`next_p2p_hand` on a voided hand takes `stacks = list(self._hand_stacks)`
(`session.py:2074-2076`), the **pre-hand** stacks, and redeals the same seats
at the same button. Every `VOID_*` outcome therefore refunds everything
committed during the hand — including to the seat the void blamed. A seat that
dislikes its holes can equivocate at `author_seq` and buy a mulligan; the cost
is attribution, not chips.

This is a standing-invariant-1 (non-profitability) leak that **predates M2 and
is not created by it**. It is recorded here because M2's design deliberately
routes disconnect and recovery *away* from the void path, and the size of that
decision is only visible once you know what the void path does with chips. Fix
is out of M2's scope; carried as a ROADMAP follow-up.

**F-4 — DLEQ proofs carry no session or hand binding.**

`dleq._challenge` (`dleq.py:51-57`) hashes `poker.dleq.v1| G X D C0 R1 R2` —
no `session_id`, no `hand_no`. A `deal_share` or audit share is therefore
transplantable to any context where the same `(X, C0)` pair recurs. Bounded in
practice: `X` is derived from `session_id` through `derive_share`, and `C0`
carries per-shuffle randomness, so recurrence across hands is not reachable
without a randomness failure. Not a live hole; named because a recovery design
that replays stored shares must not be the thing that makes `(X, C0)` recur.
It does not: replay under this design re-emits stored envelopes into the hand
that produced them, never into another.

**F-5 — the invite capability is memory-only, so failure class 2 is not
actually reachable for *any* role until it is persisted.**

Admission is a fresh handshake on every connection — `handle_disconnect`
calls `forget(conn_id)` unconditionally (`session.py:2841-2842`), which is
correct and is what makes a captured response worthless. But answering that
fresh handshake needs the invite's `admission_secret`, and nothing holds it
across a process boundary:

* a **joiner** parsed it out of the room code the user typed
  (`invite.parse_room_code`), and holds it only in memory;
* a **host** generated it in `generate_room_code` (`invite.py:131-142`), and
  holds it only in memory. On restart it would mint a *new* secret, and every
  invite already in players' hands would stop verifying — the host would be
  locked out of its own table.

So the first draft's reconnect sequence was complete for class 1 and
incomplete for class 2, in a way that reading the sequence does not reveal:
every step is right, and the peer cannot reach step 2. This is the reason
`RECOVERY_SPEC.md` §6.7 exists, and the reason it is honest about writing one
capability to disk (§6.5) rather than claiming the journal holds nothing
sensitive.

Bounding what that capability is worth to an attacker who reads it:
`admission.py:38-44` states outright that it proves "has the invitation", not
"is entitled to seat N". Mid-hand it buys admission and nothing else —
`_on_player_info` is LOBBY-only (`session.py:2415-2420`) and `_bind_seat_keys`
is one-way (`session.py:1674-1675`) — and anyone who can read the file can
also read `identity.py`'s private key, at which point they need no capability
because they can be the peer.

**F-6 — `_table_cfg` is memory-only, and the replica cannot be rebuilt
without it.**

`ReplicaTable` is constructed from `names`, `stacks`, `sb`, `bb`, `structure`
(`replica_table.py:70-83`), sourced from `_table_cfg`
(`session.py:2004-2007`), which is set in `start_p2p_session`
(`session.py:1962-1965`) and never written anywhere else. §4's reconstruction
pivot — "resume state ≡ constructor tuple + the ordered set of envelopes" —
is exactly right, and this is a piece of the constructor tuple the first
inventory pass left out. Replaying `bet_action` envelopes against a replica
built with the wrong blinds does not fail loudly; it diverges.

---

## 2. The three failure classes, separated

The classes differ in exactly one thing — what survives — and that decides
what is recoverable.

| | Class 1 · transport interruption, process alive | Class 2 · process restart, same device | Class 3 · device or secret loss |
|---|---|---|---|
| Ed25519 identity | in memory | on disk (`identity.py`) — **see F-1** | **gone** |
| `master_secret` | in memory | on disk (`device_secret.py`) | **gone** |
| `x_share` | in memory | re-derivable from the two above + context | **not derivable** |
| received envelopes | in memory | **gone** (nothing persists) | gone |
| outbound `author_seq` | in memory | **gone** | gone |
| `_round_decks`, `_shares`, `_hole` | in memory | **gone** | gone |
| `button`, `seats_in`, `prevention` | in memory | **gone** | gone |
| seat↔key binding, seat order | in memory (survives `handle_disconnect`) | **gone** | gone |
| **Recoverable?** | yes, with reconnect only | yes, with reconnect **and** durable state | **no** |

Class 3 is terminal *for the hand* by construction, not by policy:
`MentalDeal` is n-of-n at every phase (**B1**), and the vanished seat's share
is unreproducible. No amount of peer cooperation substitutes for it, and
threshold cryptography — which would — is an explicit non-goal.

The prior note's observation that `device_secret.py` "already solves half of
failure-class 2" is right and worth restating precisely: it solves the half
that no protocol message can replace. The other half — everything derived from
messages — is recoverable *if* the messages are retained, which is what §5
compares.

---

## 3. Exact resume-state inventory

Every field a peer needs to re-enter a hand at its exact seat, classified by
how it is obtained. This is the inventory ROADMAP M2's acceptance gate asks
for, and it is the input to the mechanism comparison.

### 3.1 Cryptographic layer (`MentalDeal`)

| Field | Class | How, and the hazard if got wrong |
|---|---|---|
| `session_id` | **derivable** | from the v3 deal context. Today it is `conn_id`-dependent (**B7**), which is the whole reason reconnect fails |
| `hand_no` | **must persist** | a wrong hand number is refused by `_hand_msg_ok` (`session.py:1788-1793`) — fails closed, but the peer never rejoins |
| `seat` | **must persist** | derived today from `_seat_order.index(local_conn_id)` (`session.py:1943-1944`), which a new `conn_id` breaks outright |
| `seats_in` | **must persist** | wrong membership changes `deal_map` and the n-of-n set |
| `button` | **must persist** | **card-leak hazard, not a mismatch.** `_enter_deal` (`mental_deal.py:676-692`) broadcasts a share for every hole position it believes it does not own. Under a wrong button that set includes its **own** holes, and it publishes the share that was the only thing masking its cards. The privacy argument at `mental_deal.py:666-673` depends entirely on button agreement |
| `master_secret` | **persisted already** | `device_secret.py` |
| `_x_share` | **derivable** | `derive_share(master, session_id, hand, seat)` — deterministic (`mental_deal.py:155-174`) |
| `prevention` | **must persist, or fail closed** | defaults `False` (`mental_deal.py:201`) and is not in the transcript. A rebuilt instance that defaults it stops verifying and omits its own proof — the silent downgrade the Bayer-Groth mandate exists to prevent. It is *inferable* from the adopted deal policy, which is bound into the context, so "persist it" and "fail closed" collapse into the same thing once the policy is bound: a peer that cannot establish the policy cannot build a context at all (`session.py:887-890`) |
| `phase`, `_deck`, `_shuffle_round`, `_pubkeys`, `_shares`, `_hole`, `_board`, `_revealed_streets`, `_audit_shares` | **reconstructable** | deterministic function of the constructor tuple plus the ordered set of deal messages. This is the pivot of the whole design — see §4 |
| `_round_decks` | **reconstructable** | same; every intermediate deck was broadcast (`deck_audit.py:20-26`). Memory-only today, which is what destroys chain attribution on a crash |
| local seat's recovered hole cards | **re-derivable, never stored** | `x_share` (re-derived) applied to the reconstructed deck. They need not be written to disk at all — see §7.3 |
| shuffle witness (`perm`, `scalars`) | **never needed again** | consumed to build the proof and discarded with the local frame (`mental_deal.py:522-526`, `:537-542`). The proof is in the broadcast envelope. This is the one piece of never-broadcast state whose loss is provably harmless |
| DLEQ nonces, BG proof randomness | **never needed again** | same argument; the proof is public, the randomness is not an input to verification |

### 3.2 Session layer

| Field | Class | How, and the hazard |
|---|---|---|
| `_seat_order` | **must NOT persist** — corrected, see below | seat index → **transport hop**. The 2026-08-18 note called it "seat index → identity", which was true only while `conn_id` *was* the identity. Under v3 it is a list of dead sockets |
| `_seat_keys` | **must persist** | the immutable seat↔signing-key binding; the sole basis of exact-seat authorization (`session.py:1737-1771`). It need not be stored separately: it is field 5 of the deal-context pre-image (`RECOVERY_SPEC.md` §3.2), so the authorization bytes and the domain bytes cannot drift |
| `_table_cfg` (`names`, `sb`, `bb`, `structure`) | **must persist** | `ReplicaTable.__init__` (`replica_table.py:70-83`) takes all four, and `_begin_p2p_hand` reads them from `_table_cfg` (`session.py:2004-2007`). Memory-only today, so a restart cannot rebuild the replica at all — the betting layer's equivalent of losing `button`, though without the card-leak edge |
| `_deal_policy` | **must persist** | write-once (`session.py:1033-1066`); without it no context can be built |
| `local_conn_id` | **must NOT persist** | it is a per-socket UUID with no cryptographic content. Persisting it is the bug, not the fix. It is replaced, not restored |
| `_author_seq_out` | **must persist** | **B9.** Locally generated, never broadcast, no peer holds it. Restarting it at `AUTHOR_SEQ_START` (`session.py:1190`) re-issues numbers peers have already bound to different fingerprints, and `_author_seq_ok` (`session.py:1242-1255`) voids the hand blaming the honest returning seat |
| `_author_seq_seen` | **reconstructable** | `{author_seq: fingerprint}` per `(hand, seat)`; rebuilt by replaying retained inbound envelopes, since the fingerprint is the envelope hash (`session.py:1270-1272`) |
| `_replica` (engine, stacks, pot, `next_seq`, `phase`, `positions`) | **reconstructable** | deterministic replay of `bet_action` messages in `seq` order over the hand-start stacks; the RNG is seeded from `(session_id, hand_no)` (`replica_table.py:62-64`) |
| `_hand_stacks`, `_hand_positions` | **must persist** | the pre-hand revert point. A wrong value here is a chip error, not a protocol error |
| `_msg_buffer` (future-hand messages) | **discardable** | re-delivered or legitimately lost; losing one costs liveness, never safety |
| `_deal_outbox` | **discardable** | drained in place; anything that left it is in the journal, anything that did not was never sent |
| `_current_deadline_token`, `_deadline_started_at` | **discardable** | local clock gating only. Standing invariant 3 forbids them from changing what evidence means, so losing them cannot change an outcome |
| `terminal_state`, `terminal_record` | **must persist** | terminal state is absorbing; a restart that forgets it revives a session that ended |

**`_seat_order` keeps one non-transport job, and it has to be replaced rather
than dropped.** `_author_owns_seat` opens with
`if not (0 <= seat < len(self._seat_order))` (`session.py:1757-1758`), so the
list is also the seat-count bound. A recovering peer that simply left it empty
would refuse every seat in the session it just recovered. The seat count is in
the context pre-image, so the fix is a placeholder table of the right length
(`RECOVERY_SPEC.md` §5.2) — with the constraint, which is easy to miss, that a
placeholder must never equal `local_conn_id`: `session.py:1759-1760` returns
`True` for the local hop before any key is consulted, so a colliding
placeholder would authorize an absent seat.

**The receiver-side replay/equivocation binding is the subtle entry.** It is
listed as reconstructable, and that is true only if the retained envelopes are
the *complete* set this peer received. A peer that retains a strict subset
rebuilds a strict subset of `_author_seq_seen` and may therefore fail to
re-detect an equivocation it once saw. That is not a regression: a peer that
had crashed before receiving the second envelope would be in the same position,
and so would any peer the relay never forwarded it to. Detection of
equivocation was never guaranteed to be uniform across peers; it is
*attributable when observed*. Recovery must not claim more than that.

---

## 4. The reconstruction pivot

Both `MentalDeal` and `ReplicaTable` are **deterministic state machines over
their inbound messages**, given a small constructor tuple. That is what makes
recovery tractable at all, and it is worth stating as a claim with its
evidence, because the entire mechanism comparison in §5 depends on it.

* `MentalDeal.handle` mutates state only from message content plus
  `(session_id, hand_no, seat, seats_in, button, master_secret, prevention)`.
* Message *order* does not matter: messages that cannot yet be acted on are
  held and replayed as state advances (`mental_deal.py:239-241`, bounded by
  `MAX_HELD` at `mental_deal.py:130`).
* `_maybe_emit_shuffle` **changes no local state** (`mental_deal.py:516-526`);
  the deck is applied uniformly when the echo arrives, so the emission path is
  not part of the state transition.
* `ReplicaTable` applies `(seq, seat, action, amount)` in total order, buffers
  later seqs and drops earlier ones (`replica_table.py:33-40`).

**Therefore: resume state ≡ constructor tuple + the ordered set of envelopes.**
Nothing else needs to be captured, and in particular no secret needs to be
written to disk beyond the one already there.

**The one asymmetry, and it is load-bearing.** Replaying inbound messages
regenerates *inbound-derived* state exactly, but re-running the machine also
makes it re-*produce* its own outbound messages — and shuffling draws fresh
randomness, so a re-produced `deck_round` is a **different deck** at the same
`author_seq`. That is self-inflicted equivocation, arriving by the same door as
B9 but for a different reason. Any replay design must therefore suppress
re-emission and re-send the **stored bytes** instead. §7.4 specifies how; the
important point here is that the requirement falls out of the model rather than
being bolted on.

---

## 5. Mechanism comparison

Three mechanisms, as the ROADMAP names them.

* **WAL** — a local, durable, append-only journal of envelopes, fsynced
  **before** the corresponding frame is sent.
* **Retained authenticated transcript** — the same envelopes kept in memory
  only, with their signatures intact. No disk.
* **Peer replay** — on return, ask other peers to re-send the hand's history.

### 5.1 Coverage by failure class

| | WAL | retained transcript | peer replay |
|---|---|---|---|
| Class 1 · transport interruption | yes | **yes** | yes, but unnecessary |
| Class 2 · process restart | **yes** | no — memory is gone | partial; see §5.3 |
| Class 3 · secret loss | no (nothing does) | no | no |

### 5.2 Threat coverage

Each threat the ROADMAP acceptance gate names, against each mechanism.

| Threat | WAL | retained transcript | peer replay |
|---|---|---|---|
| **omission** (history withheld) | immune — the peer holds its own copy | immune | **fails.** A relay that omits is indistinguishable from "nothing was sent". `author_seq_holes` (`session.py:1288-1292`) states the blind spot outright: a suppressed *tail* leaves no higher number to reveal the gap |
| **reordering** | immune — replay is order-insensitive (§4) | immune | immune |
| **equivocation** | detected on replay if both envelopes were journalled; §3.2 bounds the claim | same | **worse** — the replaying peer chooses which of two envelopes to hand over |
| **stale-hand injection** | refused: `_hand_msg_ok` drops `h < _hand_no` (`session.py:1788-1793`) | same | same, **but** peer replay wants a relaxed freshness window to deliver old envelopes at all, and relaxing it on network ingress is exactly what re-opens this |
| **future-hand injection** | buffered, bounded, replayed at that hand (`session.py:1788-1792`) | same | same |
| **seat substitution** | refused by `_author_owns_seat` against the frozen `_seat_keys` | same | same |
| **key substitution** | refused: `_seat_keys` is one-way (`session.py:1674-1675`) and `_adopt_signing_key` is write-once (`session.py:2382-2392`) | same | same |
| **reconnect under a new `conn_id`** | solved only by severing `conn_id` from the domain (**B7**); no journal helps | same | same |
| **a relaying peer lying about history** | **cannot** — every journalled envelope is signature-verified and self-authored; a liar can only omit | same | **fails for completeness.** Authenticity holds (signatures), completeness does not |
| **loss of never-broadcast state** | survivable: witness and nonces are provably never needed again (§3.1); `author_seq_out` is journalled; holes are re-derived | survivable for class 1 | **fails** — no peer holds `author_seq_out`, and no peer can return your hole cards |
| **torn writes** | **must be designed for** — the only mechanism with this exposure | n/a | n/a |
| **secrets at rest** | **must be designed for** — but the design can be "store no plaintext secret at all" (§7.3) | n/a | n/a |

### 5.3 Reading the table

**Peer replay is not an evidence source.** It fails on omission and on
completeness, and its natural implementation — accepting old envelopes from
the network under a relaxed freshness rule — is the single change that would
reintroduce cross-session replay. It survives only in a strictly weaker role:
a returning peer may *request* retransmission, and peers may re-emit their own
stored envelopes byte-for-byte. Those are self-authored and
signature-verified, so a relaying host can omit them but cannot forge them, and
omission must fail closed into suspension. Peer replay is a **liveness aid with
no authority**, and calling it a recovery mechanism would overstate it.

**The retained transcript is not a separate mechanism.** It is the in-memory
view of the journal. Adopting it "instead of" a WAL buys class 1 only, and
class 1 is the failure that reconnect alone already handles once B7, B8 and B9
are fixed — the process never lost anything. So the transcript is necessary
bookkeeping, not a choice.

**The WAL is the only mechanism that reaches class 2**, which is the failure
users actually experience (app crash, laptop sleep, OS update). It carries two
exposures nothing else has — torn writes and secrets at rest — and both are
answerable by design rather than by acceptance:

* **Torn writes** are answered by ordering (**journal-then-send**) plus
  checksummed, length-prefixed records and truncation of a bad tail. The
  ordering is what makes truncation safe: a record on disk may or may not have
  been sent, but anything sent is certainly on disk. Send-then-journal has the
  opposite and fatal property — a peer could emit an `author_seq` it has no
  record of, and after restart re-issue that number for different content,
  which is B9 arriving through the recovery mechanism itself.
* **Secrets at rest** are answered by storing no card plaintext and no secret
  scalar. Everything of that kind is either a public envelope or re-derivable
  from the device secret that is already on disk. One exception survives and
  is not smoothed over: the invite's admission capability, which F-5 shows
  class-2 recovery cannot proceed without. The journal's sensitivity is
  therefore bounded by `device_secret.py`'s existing, stated threat model
  (`device_secret.py:16-25`) — an attacker who can read the journal already
  holds the master secret and the signing key, so the capability adds nothing
  to what they can do.

### 5.4 Recommendation

Adopt the **WAL**, with the retained transcript as its in-memory view, and
admit peer replay only as an unauthoritative liveness aid. This is what
`docs/RECOVERY_SPEC.md` §6-§7 specifies.

Rejected alternative, recorded because it is the obvious one: **snapshot
`MentalDeal` directly** rather than replaying envelopes. It loses on three
counts. It must serialize `_x_share` — a secret scalar — putting key material
at rest for no gain, since the scalar is deterministically re-derivable. It
must serialize Ristretto points and an evolving private dataclass, so the
on-disk format tracks an internal structure that has no stability contract.
And it captures conclusions rather than evidence: a snapshot cannot be
re-verified, whereas a journal of signed envelopes can be re-checked in full
on every recovery. Replay is slower and strictly more trustworthy.

---

## 6. Why suspension must not reuse the void path

`VOID_PEER_LOST` exists today, and mapping a disconnect to it is the obvious
implementation. It is wrong, and F-3 is why: a void reverts stacks to the
pre-hand snapshot (`session.py:2074-2076`). Under that mapping, **disconnect
becomes a refund primitive** — a player facing a bad hand pulls the network
cable and recovers everything committed. That violates standing invariants 1
and 4 directly, and it does so through the mechanism intended to *protect*
players from disconnects.

The alternative is not "void with stacks preserved" either: that is the exact
error `docs/ROADMAP.md` records for M3's current spec — it refunds the silent
player.

So suspension must be a **third thing**: a state in which the hand does not
progress, does not settle, and does not unwind. Chips stay where they are. That
is the design in `RECOVERY_SPEC.md` §8, and the frozen-value posture it implies
is the one the ROADMAP already accepted for B1: "frozen value is preferable to
a profitable disconnect primitive".

The corollary is uncomfortable and should be stated plainly rather than
smoothed over: **a permanently lost participant leaves real chips frozen with
no winner**. That is a bad user outcome. It is a *safe* bad outcome, and every
alternative examined either refunds a defector, pays a pot on unverified cards,
or requires threshold cryptography.

---

## 7. Host process restart: is it actually out of reach?

The first draft of this workspace recorded host restart as an unsolved
limitation. Independent review challenged that, and the challenge was right:
what the draft had established was that *no shipped code does it*, which is a
statement about the implementation, not about the design. Re-derived here from
the merged baseline.

### 7.1 What a host uniquely holds

Strip away the parts every peer shares — signing key, device secret, journal —
and the host holds exactly three things a joiner does not.

| Held | Survives restart today? | Consequence if lost |
|---|---|---|
| the **pinned identity** every invite names (`invite.py:35-41`) | **yes**, `identity.json` | none — this is the one thing that must not change, and it is already the one thing on disk |
| the **admission capability** (`admission_secret`, `discovery_token`) | **no** (F-5) | every existing invite stops verifying; joiners cannot re-admit |
| the **listener** and its bound port | **no** | joiners cannot reach it, even with a valid invite |

Nothing in that table is cryptographic state, and nothing in it is
irreproducible. Two are configuration; the third already persists.

### 7.2 The argument that host restart is impossible, and where it fails

The intuition behind "impossible" is the migration prohibition: host authority
cannot move, because the invite pins one exact key
(`session.py:2884-2911`). That is sound — and it is an argument about *which
key*, not about *which process*. A restarted host presents the **same** key,
so `mark_host_authenticated`'s 32-byte comparison (`session.py:1336-1353`)
accepts it for the same reason it would accept it before the restart. The pin
that forbids migration is what *permits* resumption.

The second intuition is that the host holds authoritative game state that a
restart destroys. Under the hostless design it does not. The host is a
courier: it forwards the eight `_HOSTLESS_PAYLOAD_TYPES`
(`session.py:106-110`) byte-for-byte and gains no authority over them, which
is the whole basis on which `TOPOLOGY_DECISION.md` §4 chose the star. The deal
and betting layers are peer-symmetric replicas, so the host's own state is a
*seat's* state, recovered by the same replay as any other seat's (§4).

So the residue is F-5 plus a socket. Both are answerable, and
`RECOVERY_SPEC.md` §9.7 answers them.

### 7.3 What genuinely does not survive

Reachability. The invite carries an address discovered by STUN
(`invite.py:144-149`), and a restart that lands behind a different NAT mapping
leaves joiners holding a code that routes nowhere. The host can regenerate a
code with the same token and secret — `generate_room_code` accepts both for
precisely this case (`invite.py:114-120`) — but delivering it to the players
is out of band and outside the protocol.

That is a liveness failure, not a safety one, and the distinction is not a
consolation: while it persists, every replica holds the same frozen suspended
position, nothing settles, and no chip moves.

---

## 8. Giving up: why a local timer must not be a terminal transition

The first draft let a local grace deadline drive the session into
`BLOCKED / UNRECOVERABLE`, and justified it with an argument that looked
sound: the state allocates no value, so two replicas that give up at different
moments still hold identical chips. Independent review rejected it. The
rejection is correct, and the reason is worth recording because the original
argument is the one a reader is likely to reconstruct.

**What the argument proved, and what it did not.** It proved chip agreement.
It did not prove *lifecycle* agreement, and lifecycle is not decoration here:
`BLOCKED / UNRECOVERABLE` is absorbing. Once one replica enters it, that
replica will never accept the returning seat. So two replicas holding
byte-identical signed evidence could reach permanently different verdicts
about whether the hand may still be played — decided by nothing but whose
timer was shorter. Standing invariant 3 says the final replicated outcome must
be a deterministic function of signed evidence; a private timer choosing
between "resumable" and "never" is exactly the thing it forbids.

**The sharper version of the objection.** A five-second Wi-Fi drop — failure
class 1, the most recoverable event in this document — could be converted into
a permanent loss by whichever peer had the most aggressive timeout. The
mechanism intended to answer disconnects would be manufacturing them.

**The three candidate repairs.**

| Candidate | Verdict |
|---|---|
| a **grace period bound into the deal context**, so every replica gives up at the same nominal moment | rejected. It makes the clock evidence, which is invariant 3 again in a costume; peers' clocks differ, and M4 owns timeout certificates in any case. It would also drag the M3 timeout contract into M2, which is a non-goal |
| **authenticated evidence of loss** — the affected seat signs a declaration | **adopted.** `RECOVERY_SPEC.md` §8.6. It is deterministic, replicated, clock-free, and grants no power a silent peer does not already have, since the deal is n-of-n and refusing to contribute freezes the hand anyway |
| **local stand-down that creates no shared state** | **adopted**, for everything the declaration cannot cover. The process stops; the journal keeps the hand suspended; a later start re-enters `SUSPENDED` and may resume |

**What the pair does not cover, stated rather than papered over.** Total device
loss produces no declaration, because the peer that would sign it is gone.
Those tables stay `SUSPENDED` — chips frozen, identical to
`BLOCKED / UNRECOVERABLE` in every value-bearing respect — and no mechanism
short of threshold cryptography or an external authority changes that. The
honest form of the M2 result is therefore: *the chip position is always
determined; the lifecycle label is determined whenever evidence exists to
determine it.*

**One consequence worth naming for the implementer.** Because a peer with an
unreadable journal cannot derive its next outbound `author_seq` (spec §7.6),
the declaration must sit outside the sequenced stream. Numbering it would make
the one message a lost peer needs to send collide with a number its peers have
already bound to a different fingerprint — read as equivocation, ending in a
`VOID_*` that refunds the very seat that just declared itself gone (F-3). The
requirement falls out of the failure it is being sent *from*.

---

## 9. What this analysis does not establish

* **Nothing here was executed.** No test was run, no vector was computed, no
  timing was measured. Every claim is a citation. The pinned pre-image digest
  for deal-context v3 is deliberately **not** stated in the spec for this
  reason — a hand-computed SHA-256 that nobody ran is worse than an honest
  placeholder, because it would be committed as if it were verified.
* **F-1 is read, not reproduced.** The corrupt-`identity.json` path is a code
  reading. It needs a control that writes a truncated file and observes a new
  public key.
* **The relative cost of replay-on-recovery is unmeasured.** Re-verifying every
  Bayer-Groth proof in a hand's journal is the dominant term and is not free
  (`docs/BG_SHUFFLE_BENCHMARK.md` has the per-proof figures; nobody has
  multiplied them by a hand's round count in this context). If it proves
  unacceptable, the mitigation is to trust *locally journalled, previously
  verified* proofs — which is a real weakening and would need its own analysis,
  not a quiet optimisation.
* **Host restart is designed, not demonstrated.** §7 argues from the merged
  baseline that nothing blocks it; that argument is a code reading like every
  other claim here. The spec's C32 is what would turn it into a result.
* **The cost of a wrong `journal_version` decision is unmeasured**, because
  there is only one version. The fail-closed rule (spec §6.2) is a design
  choice made before any migration exists to test it against.
* **The `L + 2Δ` fairness leak from the timeout research is untouched.** M2
  binds the parameters; it does not change what they mean.

---

## 10. Findings carried to the ROADMAP

| ID | Finding | Disposition |
|---|---|---|
| F-1 | `identity.py` silently regenerates identity on a corrupt file; non-atomic write | in M2 scope — recovery depends on it; spec §5.4 |
| F-2 | `DeadlineToken.actor` is a `conn_id` | binding fixed by M2; semantics owned by M3/M4 |
| F-3 | a void refunds the blamed seat | **out of M2 scope**; carried follow-up |
| F-4 | DLEQ proofs carry no session/hand binding | not a live hole; recorded so a future replay design does not create one |
| F-5 | the invite's admission capability is memory-only, so no role can re-authenticate after a restart | in M2 scope — class 2 is unreachable without it; spec §6.7, and the sensitivity argument in §6.5 |
| F-6 | `_table_cfg` is memory-only, so the replica cannot be rebuilt after a restart | in M2 scope — part of the constructor tuple; spec §6.8 carries it in `HAND_OPEN` |
