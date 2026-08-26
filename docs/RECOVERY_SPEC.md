# Suspension, Reconnect and Crash Recovery — Contract Specification

**Type:** normative contract (see `docs/COLLABORATION.md`, "Research versus
contract"). Implementation that contradicts this document is a bug in the
implementation.
**Milestone:** M2. **Base:** `4b5e85a` (`main`, post-M0, post-M1).
**Research input:** `docs/research/m2-recovery-mechanism-threat-analysis.md`
and `docs/research/p2-suspension-reconnect.md`. Those are evidence; this is the
contract. A candidate design in a research note became binding **here**, in
this document, in a reviewed pull request — never silently.

**Status while PR #41 is open: proposed contract under review.** It is
normative on merge. Nothing in this file has been executed; §12 lists the
controls that must pass before the implementation is accepted, and §13 lists
what remains unproven.

---

## 0. Scope

**In scope.** Three failure classes and their outcomes; the state a peer must
retain to resume at its exact seat; a durable recovery journal; an
authenticated exact-seat reconnect protocol; a deterministic `SUSPENDED` state
with enumerated exits; the deal-context version break from 2 to 3, carrying the
stable seat identity (B7) and the M1 rules-profile binding.

**Out of scope, and this document must not be read as deciding them.**
Threshold cryptography of any kind. The M3 timeout *contract* — §3 binds
`T`, `Δ`, `L` and `timeout_policy_version` as **bytes in a pre-image** and
takes no position on what they mean; the semantics currently in force are
known wrong (ROADMAP **B4**) and M3 owns them. M4 timeout certificates. The M6
production ticker. M7 engine conformance. Membership shrink, eviction, and the
refuted sole-live-player settlement exception. Broad admission changes: the
threat analysis did not show one is required, so none is made.

### 0.1 Blocker resolutions

| Blocker | Resolution | Where |
|---|---|---|
| **B1** — `MentalDeal` is fixed-membership n-of-n | **not removed.** Accepted as permanent without threshold crypto. Its consequence is specified rather than left as an undefined stall: permanent loss of a required cryptographic participant ends `BLOCKED / UNRECOVERABLE` with chips frozen | §1, §8 |
| **B2** — nothing persists | durable recovery journal | §6, §7 |
| **B7** — `conn_id` in the crypto domain | stable seat identity; deal context v3 | §2, §3, §4 |
| **B8** — envelopes expire, so stored transcripts cannot be replayed | freshness becomes type-classed, conditional on stronger anti-replay evidence being present. Envelopes are **never** re-signed | §7.5 |
| **B9** — honest re-sends look like equivocation | retransmission re-emits the **stored bytes**; `author_seq` is derived from the journal, so it never restarts | §7.4, §7.6 |

---

## 1. Failure classes and permitted outcomes

Three classes, distinguished by what survives. Every recovery decision in this
document is a function of which class applies.

| Class | Event | What survives | Permitted outcomes |
|---|---|---|---|
| **1** | transport interruption, process alive | everything in memory | `SUSPENDED` → resume at the exact seat |
| **2** | process restart, same device | `identity.json`, `device_secret`, the journal | `SUSPENDED` → resume at the exact seat |
| **3** | device or secret loss | nothing seat-specific | `SUSPENDED` → `BLOCKED / UNRECOVERABLE` |

**The governing rule.** Permanent loss of any original cryptographic
participant before cryptographic completion of the hand MUST end
`BLOCKED / UNRECOVERABLE`, regardless of how many seats remain poker-live.
An implementation MUST NOT, on that path:

* silently resume the hand without the lost participant;
* void the hand;
* refund committed chips;
* award the pot to a sole remaining live seat.

Standing invariant 2 is the reason: a seat that folds stops making poker
decisions but remains a **required cryptographic participant** until the hand
is cryptographically complete. "How many players are still in the pot" is not
an input to this rule.

---

## 2. Stable seat identity (B7)

### 2.1 Definition

A seat's **stable identity** is the raw 32-byte Ed25519 public signing key
frozen for that seat index by `Session._bind_seat_keys()`
(`holdem/p2p/session.py:1623-1710`) — the value held in `_seat_keys[seat]`,
decoded from hex to bytes.

It is the key `_author_owns_seat` (`session.py:1737-1771`) already uses to
authorize every seat-scoped message. M2 introduces **no new identity**; it
promotes an identity the protocol already trusts for authorization into the
cryptographic domain that `conn_id` wrongly occupied.

### 2.2 Canonical encoding

```
seat_identity := kind : u8
              || length : u32be
              || body : length octets
```

| `kind` | Name | `body` |
|---|---|---|
| `0x01` | `SEAT_ID_ED25519` | the raw 32-byte public key. `length` is exactly `0x00000020` |
| `0x02` | `SEAT_ID_COMPAT_CONN` | the seat's `conn_id`, UTF-8 |

`kind` is bound as its own octet so the two forms can never produce the same
byte string. A `0x02` identity is legal **only** in `AUTHOR_MODE_COMPAT`
(in-memory harnesses and benchmarks, which carry no envelopes and therefore
have no seat keys at all). In `AUTHOR_MODE_WIRE` an implementation MUST refuse
to build a deal context containing any `0x02` identity, and MUST refuse rather
than substitute. This is reachable-by-construction only as a bug:
`_bind_seat_keys` already raises in wire mode on an empty or partial map
(`session.py:1692-1709`), so a complete set of `0x01` identities exists before
any hand begins.

Keys are compared and encoded over all 32 bytes. No prefix comparison anywhere.

### 2.3 Lifecycle

| Stage | What happens | Evidence |
|---|---|---|
| **mint** | Ed25519 keypair generated on first launch and persisted | `identity.py:42-80` |
| **assert** | joiner signs `player_info`; the host takes the key from the **verified envelope**, never from the payload | `session.py:2430-2434` |
| **pin to connection** | admission binds `conn_id → key`; later traffic signed by a different key is refused | `session.py:1466-1489` |
| **freeze** | `_bind_seat_keys` builds `seat → key` once, all-or-nothing, one-way | `session.py:1674-1710` |
| **authorize** | every seat-scoped message is checked against it | `session.py:1737-1771` |
| **survive disconnect** | `handle_disconnect` clears admission and roster state but touches neither `_seat_keys` nor `_seat_order` | `session.py:2824-2866` |
| **retire** | at session end. There is no rotation, no rebinding, and no mid-session change | `session.py:1674-1675` |

### 2.4 Proof: it survives reconnect

1. The private key is on disk and is loaded unchanged on the next launch
   (`identity.py:54-56`), so the public key is byte-identical across a restart.
   **Subject to §5.4** — this property is currently defeatable by a corrupt
   `identity.json`, and §5.4 is normative about fixing that.
2. `handle_disconnect` does not clear `_seat_keys` (§2.3), so the surviving
   peers' binding is unchanged by the departure.
3. `_author_owns_seat` reads only `(seat, author)`; `conn_id` participates only
   in the self-delivery shortcut (`session.py:1759-1760`) and the compat
   fallback (`session.py:1771`).
4. Under v3 the deal context contains no `conn_id` (§3), so a new hop changes
   no digest, hence no `session_id`, hence no `x_share`, hence no announced
   `X`.

Therefore the same peer returning on a new socket produces the same
authorization verdict and the same key share. The abort at
`mental_deal.py:438-439` — "seat N announced conflicting key shares", which
today blames the honest returning peer — becomes unreachable by reconnect
alone.

### 2.5 Proof: it cannot be substituted

Four independent write-once gates, all already merged:

* `_adopt_signing_key` (`session.py:2382-2392`) refuses to move an established
  key, and does so for **every** roster path, not only the host's entry.
* `_bind_seat_keys` is idempotent and one-way (`session.py:1674-1675`); once
  populated it is never rebuilt.
* `_on_player_info` is host-only and LOBBY-only (`session.py:2404-2420`).
* `_on_player_list` is joiner-only and host-hop-gated
  (`session.py:2459-2474`), and closes the invite-pin chain for the host's own
  seat (`session.py:2489-2532`).

**Residual, inherited and not closed by M2.** A malicious host can seat a key
that never completed admission, and can assert one key for two seats. Both are
stated non-guarantees of the existing design (`admission.py:22-44`;
`_bind_seat_keys`'s own docstring, `session.py:1665-1672`). M2 depends on the
binding being *immutable*, which it is; it does not depend on the binding being
*independently attested*, which it is not. Closing this needs per-seat
capabilities at admission time and is not in scope.

---

## 3. Deal context version 3

### 3.1 The version change

`Session._DEAL_CTX_VERSION` (`session.py:857`) changes **from `2` to `3`**.
Exactly that: one deliberate break, not two accidental ones (ROADMAP
Decisions, 2026-08-18). The version is encoded **inside** the pre-image, as it
is today (`session.py:894`), so a v2 and a v3 pre-image cannot collide.

`_deal_session_id()` continues to return
`f"poker.deal.v{_DEAL_CTX_VERSION}:" + sha256(pre-image).hexdigest()`
(`session.py:930-931`), so the visible prefix becomes `poker.deal.v3:`.
`docs/TIMEOUT_SPEC.md` shows `poker.deal.v2:` in an example; that example goes
stale here and is corrected by M3, which owns that file. M2 does not edit it.

### 3.2 Canonical field order and encodings

Every field is fixed-width or length-prefixed, so the encoding is injective —
the property `session.py:859-886` records paying for once already. Field order
is **normative**; a different order is a different protocol and MUST be a
further version bump.

| # | Field | Type | Encoding |
|---|---|---|---|
| 0 | domain label | bytes | `u32be(18)` then the 18 octets of `poker.deal.context` |
| 1 | layout version | int | `u32be(3)` |
| 2 | deal policy | UTF-8 | `u32be(len)` then the octets |
| 3 | **rules profile** | UTF-8 | `u32be(22)` then the 22 octets of `poker.tda.2024.nlhe.v1` |
| 4 | seat count | int | `u32be(n)` |
| 5 | seat identities | — | `n` entries **in seat-index order**, each encoded per §2.2 |
| 6 | `timeout_policy_version` | int | `u32be` |
| 7 | `T` | milliseconds | `u64be` |
| 8 | `Δ` (delta) | milliseconds | `u64be` |
| 9 | `L` | milliseconds | `u64be` |

### 3.3 The rules-profile field

M1 froze the identifier and its encoding (`POKER_RULES_PROFILE.md` §1.1-1.3)
and left the placement to M2 (§1.4). This is the placement.

```
field 3   00 00 00 16 | 70 6f 6b 65 72 2e 74 64 61 2e 32 30 32 34 2e
                        6e 6c 68 65 2e 76 31
```

It is **one field**: a four-octet big-endian length followed by its 22 UTF-8
octets. An implementation MUST NOT:

* split it across fields, or bind any component of the grammar separately;
* parse it, case-fold it, trim it, or apply Unicode normalisation before
  binding;
* substitute a hash, digest, or numeric code for the string;
* omit it, or bind a default when the profile is absent — a session with no
  profile MUST refuse to build a context, exactly as it already refuses with no
  adopted deal policy (`session.py:887-890`).

It is bound **beside** the deal policy (field 2) and the seat order (fields
4-5), not folded into either.

### 3.4 Timing fields

`T`, `Δ` and `L` are **unsigned 64-bit big-endian integers in milliseconds**.
Not floats: a float has no canonical cross-peer encoding, and a value that two
peers serialize differently would split the digest for two honest peers.
`DEFAULT_PHASE_TIMEOUTS` (`timeout.py:75-82`) holds floats today; conversion
MUST be exact — a value that is not an integral number of milliseconds is
**refused**, never rounded.

`timeout_policy_version` is a `u32be` naming which timeout *semantics* the
table runs under.

* `0` — **no ratified timeout contract.** The semantics in force are the
  pre-M3 implementation, known to be incorrect (ROADMAP **B4**). This is the
  value M2 binds, and it is a legal, meaningful value that MUST always be
  present. It is not "absent" and MUST NOT be treated as a default.
* `1` and above — assigned by M3 when a corrected contract exists.

Binding `0` is deliberate. It means a peer running future M3 semantics derives
a different domain from a peer running today's, so the two cannot deal a hand
together. M2 refuses to bless the known-wrong semantics by giving them a
version number that implies ratification, and refuses to leave the field out,
which would let the M3 change happen silently.

**`G` and `E` are derived, never bound and never transmitted.**

```
G = L + Δ
E = T + G
```

An implementation MUST NOT place `G` or `E` in the pre-image, and MUST NOT
accept either as a wire field. Binding a derived value alongside its inputs
creates two sources of truth for one quantity, and the failure mode is a peer
that agrees on `T`, `Δ` and `L` while disagreeing on `E`.

### 3.5 Worked pre-image layout

A two-seat wire-mode table. Symbolic where the value is a key.

```
field 0  00 00 00 12  70 6f 6b 65 72 2e 64 65 61 6c 2e 63 6f 6e 74 65 78 74
field 1  00 00 00 03
field 2  00 00 00 0e  62 61 79 65 72 2d 67 72 6f 74 68 2d 76 31
field 3  00 00 00 16  70 6f 6b 65 72 2e 74 64 61 2e 32 30 32 34 2e
                      6e 6c 68 65 2e 76 31
field 4  00 00 00 02
field 5  01  00 00 00 20  <32 octets: seat 0 Ed25519 public key>
         01  00 00 00 20  <32 octets: seat 1 Ed25519 public key>
field 6  00 00 00 00                                    timeout_policy_version = 0
field 7  00 00 00 00 00 00 75 30                        T = 30000 ms
field 8  00 00 00 00 00 00 13 88                        Δ =  5000 ms
field 9  00 00 00 00 00 00 13 88                        L =  5000 ms
```

Total pre-image length for `n = 2`: **176 octets**
(`22 + 4 + 18 + 26 + 4 + 2×37 + 4 + 24`).

**No SHA-256 digest is pinned in this document.** Nothing here was executed
(§13), and a hand-computed constant committed as though verified is worse than
no constant. The implementation MUST add a vector test that pins the pre-image
**bytes** for a fixed input — those are checkable by inspection against the
table above — and separately pins the digest, generated once and committed with
the break controls of §12 proving it discriminates.

---

## 4. Every context derived from `_deal_session_id()`

The version break propagates to all of these simultaneously. This enumeration
is normative: an implementation that changes the context MUST confirm each
entry, and a new consumer MUST be added here.

| # | Consumer | Site | What breaks if the two peers disagree |
|---|---|---|---|
| 1 | `MentalDeal.session_id` | `session.py:1136-1145` → `mental_deal.py:195` | everything below |
| 2 | `derive_share` HKDF info `poker.share.v1\|{sid}\|{hand}\|{seat}` | `mental_deal.py:155-174` | different `x_share` → different `X` → conflicting-key-share abort (`mental_deal.py:438-439`) |
| 3 | PoP context `poker.dkg.v1\|{sid}\|{hand}\|{seat}` | `mental_deal.py:177-179`, verified at `mental_deal.py:434-436` | key-share proof-of-possession fails |
| 4 | Bayer-Groth statement context `poker.mentaldeal.bg.v1\|{sid}\|{hand}\|{round}\|{seat}\|{ck seed}` | `mental_deal.py:502-514` | shuffle proof fails to verify |
| 5 | `ReplicaTable` RNG seed `replica.v1\|{sid}\|{hand}\|` | `replica_table.py:62-64`, `session.py:2004-2007` | replica RNG diverges (betting never consults it today; belt and braces by design) |
| 6 | `DeadlineToken.hand_id` | `timeout.py:56, 65`, `TIMEOUT_SPEC.md` | proposals from one hand cannot be replayed into another; a mismatch makes every proposal stale |
| 7 | `Session._deal_context_id` → `TerminalRecord.session_id` | `session.py:932`, `session.py:2787-2799` | forensic records name a context nobody else recognises |
| 8 | **new in M2** — the `ctx` field on every hostless message | §7.5 | cross-session replay is refused |

Entries 1-4 are cryptographic and fail closed. Entry 5 is a determinism hedge.
Entries 6-7 are coordination and forensics. Entry 8 is new and is what makes
the freshness relaxation in §7.5 safe.

**Attribution improves, and this is worth recording because the current
docstring says the opposite.** `_deal_session_id`'s docstring
(`session.py:918-925`) accepts that a policy mismatch surfaces at DKG as a
proof-of-possession failure blaming an honest peer. With entry 8, a peer whose
context differs — for any reason, including a different rules profile — is
refused at **ingress** with the mismatch named, before any cryptographic step
runs. The trade that docstring describes is no longer being made.

### 4.1 `DeadlineToken.actor`

`timeout.py:67` types `actor` as a `conn_id`. Under v3 it MUST carry the
**stable seat identity** — the seat index, with the key available for
verification — so that a token does not name a dead transport hop after a
reconnect. This is a *binding* change only. What a timeout means, when one may
be proposed, and what it does to a hand are M3's and M4's, and M2 takes no
position.

---

## 5. Resume-state requirements

### 5.1 Must persist

An implementation MUST be able to recover all of the following after a process
restart, or MUST refuse to resume:

`hand_no`; `seat`; `seats_in`; `button`; `_seat_order`; `_seat_keys`;
`_deal_policy`; the rules profile; `timeout_policy_version`, `T`, `Δ`, `L`;
`_hand_stacks` and `_hand_positions` (the pre-hand revert point);
`prevention`; the outbound `author_seq` stream; `terminal_state` and
`terminal_record`.

`prevention` MUST NOT be defaulted on recovery. A rebuilt `MentalDeal` that
takes `mental_deal.py:201`'s `prevention: bool = False` silently stops
verifying proofs and omits its own — the exact downgrade the Bayer-Groth
mandate exists to prevent. It is recovered from the journalled deal policy;
if the policy cannot be recovered, no context can be built
(`session.py:887-890`) and the peer MUST fail closed rather than resume.

**`button` is a card-leak control, not a gameplay setting.** `_enter_deal`
(`mental_deal.py:676-692`) broadcasts this seat's decryption share for every
hole position it believes it does not own. Under a wrong `button` that set can
include its **own** hole positions, publishing the one share that was masking
its cards from the table. An implementation MUST NOT adopt a `button` from any
peer for a hand it has already opened; see §9.5.

### 5.2 Must NOT persist

`local_conn_id`. It is a per-socket UUID4 with no cryptographic content
(`transport.py`, `_new_conn_id`). Persisting it would restore the very
coupling B7 removes. It is **replaced** on reconnect, not restored.

No secret scalar (`_x_share`), no shuffle witness, no DLEQ or proof
randomness, and no card plaintext. See §7.3.

### 5.3 Reconstructed, not stored

`phase`, `_deck`, `_shuffle_round`, `_pubkeys`, `_shares`, `_hole`, `_board`,
`_revealed_streets`, `_round_decks`, `_audit_shares`, `_author_seq_seen`, and
the entire `ReplicaTable` are **deterministic functions** of the persisted
tuple plus the journalled envelopes. They MUST be rebuilt by replay (§7.4),
not by deserializing a snapshot.

The local seat's own hole cards are in this class: they are re-derived from the
re-derived `x_share` applied to the reconstructed deck. They are never written
to disk.

The shuffle witness and every proof nonce are in **neither** class. They are
consumed to build a proof and discarded with the local frame
(`mental_deal.py:516-543`), and the proof itself is in the broadcast envelope.
Their loss is provably harmless, which is why "loss of locally generated
never-broadcast state" is survivable at all.

### 5.4 Identity durability (normative, from finding F-1)

`identity.py` currently swallows every parse failure and falls through to
generating a fresh keypair, then overwrites the file
(`identity.py:45-49`, `:58-61`, `:73-74`). Its write is not atomic. A torn
`identity.json` therefore mints a new protocol identity **silently**, which
converts a recoverable class-2 restart into an unrecoverable class-3 secret
loss with no observable transition.

Exact-seat reconnect rests entirely on that key. This document therefore
requires:

* an existing `identity.json` that cannot be read, parsed, or decoded MUST
  raise, naming the file and the remedy. It MUST NOT be replaced
  automatically. This is the rule `device_secret.py:138-153` already applies to
  the other secret a seat cannot be recovered without;
* the file MUST be published atomically — write to a temporary file in the same
  directory, fsync, then publish — matching `device_secret.py:86-121`;
* generation on **first use** (no file present) is unchanged.

---

## 6. The recovery journal

A local, durable, append-only log of signed envelopes and a small number of
control records. It is the only new persistent state M2 introduces.

### 6.1 Ordering rule — journal, then send

An outbound frame MUST be appended and fsynced **before** it is handed to the
transport. An inbound frame MUST be appended and fsynced before it is applied
to protocol state.

This ordering is what makes torn-tail truncation safe. It gives:

> a record on disk may or may not have been sent; anything sent is certainly
> on disk.

Send-then-journal has the opposite property, and it is fatal rather than
merely untidy: a peer could emit an `author_seq` it has no record of, and after
restart re-issue that number for different content — which is **B9** arriving
through the recovery mechanism itself.

### 6.2 Record format

```
record := length : u32be          # of type ‖ payload
       ‖ type   : u8
       ‖ payload: length-1 octets
       ‖ digest : 32 octets       # SHA-256 over (length ‖ type ‖ payload)
```

| `type` | Name | Payload |
|---|---|---|
| `0x01` | `HAND_OPEN` | the deal-context pre-image (§3.2) verbatim, plus `hand_no`, `local_seat`, `button`, `seats_in`, `prevention`, `_hand_stacks`, `_hand_positions` |
| `0x02` | `OUTBOUND` | the exact packed envelope bytes, as handed to the transport |
| `0x03` | `INBOUND` | the exact received envelope bytes, as verified |
| `0x04` | `HAND_CLOSE` | the hand outcome record |
| `0x05` | `TERMINAL` | the `TerminalRecord` |

### 6.3 Torn writes

On open, records are read forward. A record is **valid** only if its declared
length fits in the remaining file and its digest matches. At the first invalid
record the journal MUST be truncated to the end of the last valid record and
recovery MUST proceed from there. A partially written record MUST NOT be
partially interpreted, and MUST NOT be repaired.

Truncation is safe precisely because of §6.1: a lost tail record was, at worst,
sent-but-unrecorded — and by the ordering rule that state is unreachable.

### 6.4 `HAND_OPEN` is written first

`HAND_OPEN` MUST be appended and fsynced **before** the deal driver emits
anything for that hand — that is, before `driver.start()` in
`Session.begin_hand` (`session.py:1136-1147`).

This is the button-leak control. It makes the following true by construction:

* a peer with **any** journalled activity for a hand necessarily holds that
  hand's `button`, and MUST refuse any peer-supplied value that disagrees;
* a peer with **no** `HAND_OPEN` for a hand has emitted nothing and leaked
  nothing, and may safely take the hand's parameters from the resume
  handshake — it is re-entering the hand from the beginning, exactly like a
  peer that never started it.

There is no third case, so there is no state in which a returning peer must
guess.

### 6.5 Secrets at rest

The journal MUST contain no card plaintext, no secret scalar, no shuffle
witness, and no proof randomness. Every record is either a public envelope or a
public parameter, and everything secret is re-derived from
`device_secret.py`'s existing file (§5.3).

The journal's sensitivity is therefore bounded by, and adds nothing to, the
threat model `device_secret.py:16-33` already states and accepts: play money,
no custody, and an attacker who can read the config directory can already read
the process memory. **Do not reuse this design for anything of value without
revisiting that decision.**

### 6.6 Retention

A journal is scoped to a session and MAY be discarded once the session reaches
a terminal state and its `TERMINAL` record is durable. It MUST NOT be discarded
while a hand is `SUSPENDED`.

---

## 7. Retention, replay and retransmission

### 7.1 Mechanism, decided

The journal (§6) is the recovery mechanism. The retained authenticated
transcript is its in-memory view, not a separate mechanism.

### 7.2 Peer replay has no authority

A returning peer MAY request retransmission, and peers MAY re-emit their own
journalled `OUTBOUND` envelopes byte-for-byte. Those envelopes are
self-authored and signature-verified, so a relaying peer can **omit** them but
cannot forge them.

An implementation MUST NOT treat peer-supplied history as evidence of
completeness. Omission is indistinguishable from "nothing was sent" — the blind
spot `author_seq_holes` states outright (`session.py:1288-1292`) — so a
withheld history MUST fail closed into `SUSPENDED`, never into a resumed hand
with a hole in it.

### 7.3 What is replayed

Recovery reconstructs state by feeding journalled envelopes to a freshly
constructed `MentalDeal` and `ReplicaTable`, built from the `HAND_OPEN` tuple.
Both are deterministic state machines over their inbound messages
(`mental_deal.py:239-241` holds what it cannot yet act on;
`replica_table.py:33-40` applies actions in total order), so replay order does
not affect the result.

Snapshot-and-restore of `MentalDeal` is **forbidden**. It would put
`_x_share` — a secret scalar — at rest for no benefit, since the scalar is
deterministically re-derivable; it would couple an on-disk format to a private
dataclass with no stability contract; and it would store conclusions where a
journal stores re-verifiable evidence.

### 7.4 Replay must not re-emit

Re-running the deal machine makes it re-*produce* its own outbound messages,
and shuffling draws fresh randomness — so a re-produced `deck_round` is a
**different deck at the same `author_seq`**. That is self-inflicted
equivocation.

During replay the driver's send path MUST be redirected: the *k*-th outbound
message the local seat produces is replaced by the *k*-th journalled
`OUTBOUND` record for that hand, and the stored bytes are what is (re-)sent.
Matching is **positional and by message type**, not by content — content
cannot match for a re-shuffle. A type mismatch at any position is a
journal/state divergence and MUST fail closed to
`BLOCKED / UNRECOVERABLE`; it MUST NOT be repaired or skipped.

Once the journalled outbound records for the hand are exhausted, normal
emission resumes.

This is sound because `_maybe_emit_shuffle` changes no local state
(`mental_deal.py:516-526`): the deck is applied uniformly when the echo
arrives, so discarding the locally recomputed deck in favour of the stored one
leaves the machine in exactly the state every other peer is in.

### 7.5 Envelope freshness (B8) — type-classed, and conditional

`wire.unpack`'s hard ±30 s window (`wire.py:190-195`) is replaced by two
classes, decided in one place by message type.

**`FRESHNESS_STRICT`** — unchanged ±30 s. Applies to every type that is not
seat-sequenced: the admission handshake, `player_info`, `player_ack`,
`player_list`, `game_start`, chat, the admin messages, and the resume
handshake of §9.

**`FRESHNESS_SEQUENCED`** — no timestamp check. Applies to the eight hostless
payload types (`session.py:106-110`). Their anti-replay evidence is strictly
stronger than a clock window and it is signed:

* `ctx` — the v3 deal-context id, which the receiver compares **byte-for-byte**
  against its own and refuses on any difference. This is what makes replaying
  an envelope from a *different table* impossible, and it is the property the
  ±30 s window was standing in for;
* `hand` — a past hand is dropped and a future hand is buffered
  (`session.py:1788-1793`);
* `(seat, author_seq, fingerprint)` — a duplicate is dropped and a second,
  different envelope at one number is equivocation
  (`session.py:1228-1255`).

**The exemption is conditional on the replacement being present.** A message
claiming a sequenced type but missing any of `ctx`, `hand`, `seat` or
`author_seq`, or carrying one of the wrong shape, MUST be **refused** — not
quietly given the strict window. An implementation MUST NOT be able to obtain
the relaxed rule without supplying the stronger binding.

`_send_hostless` (`session.py:1164-1193`) MUST stamp `ctx` alongside `seat` and
`author_seq`, before the transport signs, so all four are covered by the
signature and a relaying host cannot alter them
(`session.py:1172-1174`). The value is the cached `_deal_context_id`
(`session.py:932`), which remains readable after termination — `session_end`
is a sequenced type and is emitted from a terminated session
(`session.py:2113-2127`).

The one path that stamps nothing is the unseated broadcast at
`session.py:1183-1187`, where the local peer has no seat to attribute. Under
this rule a sequenced type sent that way is refused by receivers. That is the
intended outcome and not a regression: such a message is already unusable
today — it carries no seat, so `_on_deal_message` drops it
(`session.py:1840-1844`) — and refusing it at the wire boundary merely makes
the existing verdict earlier and explicit.

**Envelopes are NEVER re-signed.** Re-signing would destroy the property that
made a retained transcript worth retaining. A stored envelope is replayed as
the bytes it was, or not at all.

### 7.6 Retransmission and `author_seq` (B9)

**Retransmission is byte-identical replay of a journalled `OUTBOUND` record.**
An implementation MUST NOT re-`pack` a logical message to re-send it:
`wire.pack` stamps a fresh `ts` into the signed pre-image and therefore into
the hash (`wire.py:143-151`), so a re-packed message is a *different envelope*
at the same `author_seq`, which `_author_seq_ok` correctly reads as
equivocation and voids the hand blaming the honest sender
(`session.py:1242-1255`).

With byte-identical retransmission the existing receiver logic is already
correct: `prior == fingerprint` drops the duplicate silently
(`session.py:1237-1241`).

**The outbound `author_seq` is derived from the journal, never from a
counter.** On recovery,

```
_author_seq_out[(hand, local_seat)] = 1 + max(author_seq over journalled
                                              OUTBOUND records for that key)
```

or `AUTHOR_SEQ_START` when there are none. There is no separate counter file:
the number and the envelope that used it are the same durable record, so they
cannot desynchronise. This is what stops a restarted peer re-issuing numbers
peers have already bound to different fingerprints.

**Receiver-side reconstruction is bounded, and the bound is stated.**
`_author_seq_seen` is rebuilt from journalled `INBOUND` records, so a peer
re-detects only the equivocations it durably received. A peer that crashed
before the second envelope arrived is in the same position as a peer the relay
never forwarded it to. Equivocation detection was never uniform across peers;
it is *attributable when observed*, and recovery MUST NOT be described as
guaranteeing more.

---

## 8. The `SUSPENDED` state

### 8.1 Definition

`SUSPENDED` is a **hand-scoped** state: the hand does not progress, does not
settle, and does not unwind. It is neither a session-terminal state nor a hand
outcome, and it MUST NOT be recorded as one.

It is deliberately a third thing. Mapping a disconnect to `VOID_PEER_LOST`
would refund every chip committed during the hand, because a void reverts to
the pre-hand stacks (`session.py:2074-2076`) — making disconnection a refund
primitive and violating standing invariants 1 and 4. "Void with stacks
preserved" is the other obvious mapping and is the error `docs/ROADMAP.md`
already records against M3's current spec: it refunds the silent player.

### 8.2 Entry

A hand enters `SUSPENDED` when a contribution from a required cryptographic
participant is outstanding **and** one of:

* the transport reports that participant's connection down;
* a phase deadline elapses with the hand incomplete and no applicable outcome
  rule exists — which at `n = 2` is always the case (**B3**), and at `n ≥ 3`
  is whatever M4 later decides;
* the host connection drops during `PLAYING` (§9.6).

### 8.3 Invariants while suspended

* **No chips move.** No settle, no revert, no pot award, no blind posting.
* **No silence becomes an action.** No timer converts an absence into a fold,
  check, or call.
* **No hand outcome is recorded.** `_hand_record` stays `None`.
* The journal keeps accumulating; late traffic is still verified, journalled,
  and applied.
* The deal driver and the replica are retained, not torn down.
* The local seat MUST NOT act: `send_bet_action` returns `"rejected"`.

### 8.4 Exits — exhaustive

| Exit | Trigger | Chip effect |
|---|---|---|
| **E1 · RESUME** | the absent participant completes §9 and its outstanding contributions arrive | none — committed chips stay committed, uncommitted stacks stay with their owner |
| **E2 · UNRECOVERABLE** | the local grace deadline elapses, or the local user abandons the hand | **none.** Chips are frozen exactly as committed. `BLOCKED / UNRECOVERABLE` |
| **E3 · LOCAL SHUTDOWN** | local termination | none. Existing terminal path, chips frozen as committed |
| **E4 · INTEGRITY FAILURE** | signed evidence of a cryptographic failure — equivocation, a failed proof, a failed audit — that would have voided the hand with every peer present | the existing `VOID_*` rules apply, including the stack revert |

**E4 is narrow on purpose, and the boundary is the discriminating invariant of
this whole section.** It is permitted only when the trigger is *signed evidence
of misbehaviour* whose verdict does not depend on anyone being absent. Absence
itself, a missing contribution, an elapsed deadline, and a failure to
retransmit are **not** E4 triggers and MUST NOT reach a void. Standing
invariant 5 governs the other direction too: a cryptographic integrity failure
is not a casino misdeal, and "play on" is not available for it.

There is no fifth exit. In particular there is **no** sole-live-player
settlement exit; see §11.

### 8.5 `BLOCKED / UNRECOVERABLE`

A terminal session state. Chips are frozen at their committed position: no
winner, no refund, no redeal. The `TerminalRecord` (`session.py:2787-2799`)
records the cause and the seat whose participation was lost.

**Why a local clock may gate E2, when standing invariant 3 forbids clocks from
deciding outcomes.** `BLOCKED / UNRECOVERABLE` allocates no value. It is a
refusal to produce an outcome, not an outcome. Two replicas that give up at
different moments hold **identical** chip state — the committed-but-unsettled
position — so the clock changes *when* a replica stops waiting and never *what*
it converges to. That is the only place in this protocol where a local clock
may touch a terminal transition, and it is admissible for exactly that reason.

The grace period is **local policy**. It is not bound into the deal context,
is not transmitted, and is not evidence. M3 may promote it; M2 deliberately
blesses no value for it.

**The accepted cost, stated plainly.** A permanently lost participant leaves
real chips frozen with no winner. That is a bad user outcome. It is a *safe*
one, and every alternative examined either refunds a defector, pays a pot on
unverified cards, or requires threshold cryptography — a non-goal. The ROADMAP
already accepted this posture for B1: frozen value is preferable to a
profitable disconnect primitive.

---

## 9. Exact-seat reconnect

### 9.1 Preconditions

Reconnect reuses the immutable seat↔signing-key binding of §2 and introduces
no new authority. It is available only for failure classes 1 and 2.

### 9.2 Sequence

1. **Transport reconnect.** A new `conn_id` `C'` is minted. Nothing is assumed
   from it.
2. **Full re-authentication.** The admission handshake runs from scratch
   against a fresh server nonce. This is already forced:
   `handle_disconnect` calls `_admission.forget(conn_id)` even on a terminal
   session (`session.py:2841-2842`), so a response captured from the earlier
   connection is worthless.
3. **Host pinning.** The joiner verifies `admission_accept` against the exact
   32-byte key its invite pinned, and calls `mark_host_authenticated(C')`
   (`session.py:1315-1353`).
4. **`seat_resume`** — new, joiner → host, signed, `FRESHNESS_STRICT`:
   `ctx` (the v3 context id), `hand`, `seat`, a 16-byte `resume_nonce`,
   `next_author_seq`, and `have` — the `(seat, author_seq)` set already held,
   as a per-seat highest-contiguous value plus explicit holes.
5. **Authorization.** The envelope's author MUST byte-equal `_seat_keys[seat]`.
   Not the roster, not the connection, not the nickname — the frozen binding,
   which `handle_disconnect` never cleared. A mismatch is refused. An empty
   `_seat_keys` in wire mode is refused.
6. **Hop rebinding only.** The host repoints the seat's transport hop to `C'`.
   `_seat_order` and `_seat_keys` are **not** modified. Because v3 contains no
   `conn_id`, this changes no cryptographic domain — which is the entire
   purpose of B7.
7. **`seat_resume_ack`** — host → joiner, signed, `FRESHNESS_STRICT`, echoing
   `resume_nonce` and carrying the retained absent-seat entitlement: the seat
   index, the frozen seat→key map, `seats_in`, `button`, `hand`, `ctx` and the
   deal policy.
8. **Verification before adoption.** See §9.5.
9. **Retransmission.** Peers re-emit the journalled `OUTBOUND` envelopes the
   returning peer says it lacks, byte-for-byte (§7.6).
10. **Local replay.** The returning peer replays its own journal (§7.3-7.4),
    re-emits nothing already emitted, and resumes normal operation.
11. **Exit.** Each peer leaves `SUSPENDED` via **E1** when the outstanding
    contributions from that seat arrive.

### 9.3 Returning-peer seat discovery

A returning peer MUST NOT rediscover its seat by indexing `_seat_order` with a
`conn_id` (`session.py:1123-1127`, `session.py:1943-1944`). It recovers `seat`
from its own `HAND_OPEN` record and **asserts** it in `seat_resume`, where the
host authorizes it against the frozen binding. Identity flows from the key, not
from the socket.

`local_conn_id` is re-learned as a transport fact only. `_on_player_ack`
remains LOBBY-only (`session.py:2606-2609`); M2 does **not** reopen it, and
`seat_resume_ack` carries what a returning peer needs instead.

### 9.4 Retained absent-seat roster entitlement

`handle_disconnect` destroys the roster entry (`session.py:2847-2850`) while
the seat binding survives. The host MUST additionally retain, for the life of
the session, a record that seat *N* belongs to key *K* and is currently absent,
so that a returning peer can be answered at all. That record is derived from
`_seat_keys`, which is already immutable — it is a **read** of existing frozen
state, not a new authority, and it MUST NOT be writable by any message.

### 9.5 The returning peer verifies before it adopts

On receiving `seat_resume_ack` the returning peer MUST compare every field
against what it already holds:

* if it has a `HAND_OPEN` record for `hand`, any disagreement on `ctx`,
  `button`, `seats_in`, the seat→key map, or the deal policy is a refusal. The
  peer stays `SUSPENDED` and records the conflict. It MUST NOT adopt the
  host's value. This is the button-leak control of §5.1 and §6.4: adopting a
  wrong `button` would make it publish decryption shares for its own hole
  cards;
* if it has **no** `HAND_OPEN` record for `hand`, it has emitted nothing and
  leaked nothing, and MAY adopt the parameters — it is entering the hand from
  the beginning.

§6.4 guarantees these are the only two cases.

### 9.6 Host loss in the star topology

In production only the host listens (`docs/TOPOLOGY_DECISION.md`), so host loss
severs every peer at once. Today that is immediately session-terminal during
`PLAYING` (`session.py:2852-2858`), and wire mode refuses to elect a
replacement because the invite pins exactly one host key
(`session.py:2903-2911`).

**M2 changes the timing of that termination and nothing about its authority.**
Host loss during `PLAYING` enters `SUSPENDED` (host absent) rather than
terminating immediately, and terminates as `HOST_LOST` on the E2 grace
deadline or on explicit abandon. Terminal semantics, when reached, are
unchanged.

The safety argument, precisely: migration is forbidden because a **different**
peer would inherit host authority and every joiner's pin would be wrong.
Resumption grants authority to the **same pinned key**, and
`mark_host_authenticated` already refuses anything else, comparing all 32 bytes
(`session.py:1336-1353`). Chips are frozen under either behaviour, so this
changes no chip outcome; it only preserves the option of resuming. Host
migration remains forbidden and is not reopened.

**Known limitation, not solved here.** A host *process* restart is still out of
reach: the host must rebuild its own state from its own journal and every
joiner must reconnect to it, and no shipped path drives that. §13 records it.
The tests that pin the current immediate-termination behaviour
(`tests/test_host_loss.py`) must be revisited by the implementation, not
bypassed.

---

## 10. Committed chips are never refunded

**The invariant.** No disconnect, suspension, reconnect, recovery, or
unrecoverable outcome may return a chip that has been committed to the pot.
Uncommitted stack remains the player's. (Standing invariants 1 and 4.)

**The proof obligations**, each discharged by a control in §12:

1. `SUSPENDED` performs no chip operation at all (§8.3). Nothing to refund.
2. `SUSPENDED` never reaches `_end_hand` with a `VOID_*` outcome by way of
   absence (§8.4). Only E4 reaches a void, and only on signed evidence of
   misbehaviour whose verdict is independent of anyone being absent.
3. `BLOCKED / UNRECOVERABLE` is chip-neutral: it does not call `settle`, does
   not revert to `_hand_stacks`, and declares no winner (§8.5).
4. Reconnect restores state by replay and re-derivation only; no recovery path
   writes a stack value (§7.3).
5. There is no sole-live-player settlement exit (§11).

**The known adjacent leak, named so it is not mistaken for this one.** A
`VOID_*` outcome reverts to the pre-hand stacks (`session.py:2074-2076`) and
redeals, so the seat a void *blamed* also recovers its committed chips. That is
a non-profitability leak that predates M2, is not created by it, and is not
fixed by it — it is carried as a ROADMAP follow-up. M2's contribution is to
ensure that disconnect and recovery never *reach* that path.

---

## 11. What this specification refuses

* **The sole-live-player settlement exception.** Refuted by two independent
  adversarial reviews and recorded as such in `docs/ROADMAP.md` and
  `docs/research/p2-suspension-reconnect.md`. It MUST NOT be implemented. Note
  for a later reader: one leg of the original refutation — that the prevention
  gate was transport-conditional — was closed by M0/D4. The refutation stands
  on its independent second leg: the audit is the only point in the protocol
  where a seat publishes a proven share for its **own** hole cards
  (`mental_deal.py:680-683`, `deck_audit.py:55-69`), so the exception lets a
  fold-winner be paid without ever supplying that reciprocity.
* **Membership shrink and eviction.** Unavailable: the deal is n-of-n and
  `deal_map` is keyed on `seats_in`.
* **Voiding or refunding on absence.** §8.4.
* **Re-signing a stored envelope.** §7.5.
* **Reviving parked refs `6955f4f`, `6b553a8`, or closed PR #36.**

---

## 12. Required deliberate-break controls

Every control below MUST exist, MUST fail against the named break, and the
implementation MUST record **which test fires** for each. A test that passes
against a broken implementation is a monument to a bug (standing invariant 9).

| # | Invariant | Deliberate break | Must fire |
|---|---|---|---|
| C1 | committed chips are not refunded on disconnect | route suspension to `_void_hand` | a chip-conservation control observing the pre-disconnect committed total |
| C2 | committed chips are not refunded on unrecoverable loss | make `BLOCKED / UNRECOVERABLE` revert to `_hand_stacks` | the same control at the terminal state |
| C3 | seat authority comes from the frozen key | authorize `seat_resume` from the roster, or from `conn_id`, instead of `_seat_keys` | a substituted-key resume is accepted |
| C4 | same seat, new `conn_id` resumes cleanly | restore `conn_id` into the deal-context pre-image | the returning seat derives a different `x_share` and is blamed at `mental_deal.py:438-439` |
| C5 | the rules profile is bound | omit field 3 from the pre-image | two peers holding different profiles complete a hand |
| C6 | the rules profile is bound **as bytes** | bind `sha256(profile)`, or bind the parsed grammar fields separately | the pinned pre-image vector of §3.5 |
| C7 | the version break is real | leave `_DEAL_CTX_VERSION` at `2` | a v2 peer and a v3 peer share a domain |
| C8 | `G` and `E` are derived | add `G` or `E` to the pre-image, or accept either from the wire | the pre-image vector, and a wire-field rejection control |
| C9 | timing fields are exact integers | round a non-integral millisecond value instead of refusing | two peers derive different digests from the same table settings |
| C10 | honest re-sends are not equivocation | re-`pack` instead of re-emitting stored bytes | equivocation void blaming the honest sender |
| C11 | `author_seq` survives restart | restart the counter at `AUTHOR_SEQ_START` | equivocation void after a simulated restart |
| C12 | `prevention` is never silently defaulted | rebuild `MentalDeal` after restart with the default `prevention=False` | a missing-proof abort, or a direct assertion on the rebuilt value |
| C13 | `button` is never adopted from a peer mid-hand | adopt the ack's `button` over the journalled one | the recovering seat broadcasts a share for its own hole position |
| C14 | `HAND_OPEN` precedes emission | write `HAND_OPEN` after `driver.start()` | a crash window in which activity exists with no journalled `button` |
| C15 | torn writes are truncated, not interpreted | accept a record whose digest does not match | a truncated tail changes recovered state |
| C16 | freshness relaxation is conditional | allow a sequenced type missing `ctx` to take the relaxed path | a cross-session replay is accepted |
| C17 | `ctx` is stamped and checked | drop the `ctx` stamp in `_send_hostless`, or skip the byte comparison | a `bet_action` from another table is applied |
| C18 | peer history has no completeness authority | treat a peer's replayed history as complete | an omitted tail resumes a hand with a hole |
| C19 | no sole-live shortcut | award the pot to the sole live seat on unrecoverable loss | a pot is paid with no audit |
| C20 | replay does not re-emit | let the replayed driver emit its own freshly shuffled `deck_round` | self-inflicted equivocation at the replayed `author_seq` |
| C21 | identity is never silently regenerated | restore the swallowed parse failure in `identity.py` | a truncated `identity.json` yields a new public key |
| C22 | E4 is narrow | let an elapsed deadline or a failed retransmission reach a void | absence produces a refund |

---

## 13. Known limitations

* **Nothing in this document has been executed.** No test was run and no
  digest was computed on the host that wrote it. §3.5 pins bytes, which are
  checkable by inspection; it deliberately pins no SHA-256.
* **Host process restart is not solved** (§9.6). What it would take: the host
  rebuilding from its own journal, every joiner reconnecting through §9, and a
  path that drives both. The invite pin makes it *possible* — the same pin that
  forbids migration — but no shipped code does it.
* **Replay cost is unmeasured.** Re-verifying a hand's Bayer-Groth proofs on
  every recovery is the dominant term. If it proves unacceptable, trusting
  locally journalled previously-verified proofs is a real weakening and needs
  its own analysis, not a quiet optimisation.
* **Equivocation detection after recovery is bounded** by what was durably
  received (§7.6), and this document claims no more.
* **A malicious host can still seat a key that never handshaked** (§2.5). M2
  inherits that non-guarantee and does not close it.
* **`n = 2` loses liveness rather than safety** (**B3**). Silence suspends; it
  never folds. A stalling opponent can freeze a heads-up table indefinitely,
  and no peer-only mechanism fixes that.
* **The `L + 2Δ` fairness leak** from the timeout research is untouched. M2
  binds the parameters; it does not change what they mean.
* **`VOID_*` still refunds the blamed seat** (§10). Out of scope, carried.
