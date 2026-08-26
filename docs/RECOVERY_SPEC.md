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

**Revision 2, 2026-08-26.** Independent review of revision 1 returned three
blocking findings, and this document changed rather than argued: same-device
**host process restart** is now specified (§9.7) instead of deferred as a
limitation; the **terminal transition on a local grace timer** is removed and
replaced by evidence-driven `BLOCKED / UNRECOVERABLE` plus a local stand-down
that creates no shared state (§8.4-§8.6); and `HAND_OPEN` has a canonical,
versioned, bounded encoding (§6.8) alongside a journal header and version
rule (§6.2). Two consequential omissions surfaced while making those changes
and are fixed here too: the invite capability nothing persisted (§6.7) and the
table configuration the replica is rebuilt from (§6.8).

---

## 0. Scope

**In scope.** Three failure classes and their outcomes, for every role
including the host's own restart (§9.7); the state a peer must retain to
resume at its exact seat; a durable recovery journal, specified to the octet;
an authenticated exact-seat reconnect protocol; a deterministic `SUSPENDED`
state with enumerated exits, each classified as replicated-and-evidenced or
local-only; the deal-context version break from 2 to 3, carrying the stable
seat identity (B7) and the M1 rules-profile binding.

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
| **B1** — `MentalDeal` is fixed-membership n-of-n | **not removed.** Accepted as permanent without threshold crypto. Its consequence is specified rather than left as an undefined stall: the hand suspends with chips frozen, and ends `BLOCKED / UNRECOVERABLE` — still frozen — once the loss is established by authenticated evidence | §1, §8 |
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
| **2** | process restart, same device | `identity.json`, `device_secret`, the journal, the lobby record | `SUSPENDED` → resume at the exact seat |
| **3** | device or secret loss | nothing seat-specific | `SUSPENDED` → `BLOCKED / UNRECOVERABLE` when the loss is *established* (§8.4); otherwise `SUSPENDED` indefinitely, with identical frozen chips |

**The classes are role-independent.** A host is a seat with a listener
attached, not a separate kind of participant. Class 2 therefore covers host
process restart, which is specified in **§9.7** and is not carved out as an
exception. §9.6 covers host loss as observed by the joiners; §9.7 covers the
host's own return.

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

**Permanence is a claim, and claims need evidence.** Absence is not evidence
of permanence — that is the same asymmetry standing invariant 3 states for
clocks, and it does not stop being true because the wait has been long. So
`BLOCKED / UNRECOVERABLE` is reached from **authenticated evidence** that
recovery is impossible (§8.4, E2), never from a local timer. Where no such
evidence exists, the hand stays `SUSPENDED`. The two states are
**chip-identical** — committed chips frozen, nothing settled, nothing
reverted — so the difference is legibility, not value. Every prohibition in
the governing rule holds in both.

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
| 8 | **new in M2** — the `ctx` field on every hostless message, on the §9 resume handshake, and on `seat_lost` | §7.5, §8.6, §9.2 | cross-session replay is refused; a resume or a loss declaration cannot be aimed at another table |

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

`hand_no`; `local_seat`; `seats_in`; `button`; the **seat → key vector**
(`_seat_keys`); `_deal_policy`; the rules profile; `timeout_policy_version`,
`T`, `Δ`, `L`; the table configuration the replica is constructed from
(`names`, `sb`, `bb`, `structure`); `_hand_stacks` and `_hand_positions` (the
pre-hand revert point); `prevention`; the outbound `author_seq` stream;
evidence-derived `terminal_state` and `terminal_record`; and the **lobby
record** — the invite parameters this peer needs to re-authenticate (§6.7).

Everything on that list except the `author_seq` stream, the terminal record
and the lobby record is carried by one `HAND_OPEN` record, whose canonical
encoding is §6.8. The seat → key vector is not stored twice: it **is** the
seat-identity field of the deal-context pre-image (§3.2, field 5), so the
bytes that authorize a resuming seat and the bytes that define the
cryptographic domain cannot drift apart.

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

`local_conn_id`, and `_seat_order` — the seat-indexed table of **transport
hops**. Both are per-socket UUID4s with no cryptographic content
(`transport.py`, `_new_conn_id`). Persisting either would restore the very
coupling B7 removes. They are **replaced** on reconnect, not restored.

`_seat_order` is on this list even though the 2026-08-18 research classified
it as "must persist: seat index → identity". Under v3 that description is no
longer true of it: seat index → *identity* is `_seat_keys`, and `_seat_order`
holds only seat index → *hop*. Persisting a dead hop table would give a
recovering peer a set of conn_ids that name nothing.

**What replaces it on recovery.** `_seat_order` retains one non-transport
use: `_author_owns_seat` bounds-checks `0 <= seat < len(self._seat_order)`
(`session.py:1757-1758`). A recovering peer therefore rebuilds it at the
recovered seat count with **placeholder** entries, replacing each as that
seat reconnects. Placeholders MUST be unique, MUST NOT equal `local_conn_id`,
and MUST NOT equal any live `conn_id` — a placeholder that collided with
`local_conn_id` would hit the self-delivery shortcut at `session.py:1759-1760`
and authorize an absent seat. In wire mode the placeholder participates in no
authorization decision: `_seat_keys` is non-empty, so authority comes from the
key (`session.py:1761-1764`).

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

### 6.2 File header and record format

A journal file begins with one fixed header, written and fsynced before any
record:

```
header := magic   : 16 octets     # "poker.journal.v1" in UTF-8, exactly
       ‖ journal_version : u32be  # 1
```

```
record := length : u32be          # of type ‖ payload
       ‖ type   : u8
       ‖ payload: length-1 octets
       ‖ digest : 32 octets       # SHA-256 over (length ‖ type ‖ payload)
```

`length` MUST be at least 1 and at most `JOURNAL_MAX_RECORD` = 1 048 576
octets, which exceeds `wire.MAX_JSON_BYTES` and therefore bounds every
envelope the transport can deliver. A declared length outside that range is
treated as a malformed record (§6.3), never allocated against.

| `type` | Name | Payload |
|---|---|---|
| `0x01` | `HAND_OPEN` | the canonical hand-open payload of §6.8 |
| `0x02` | `OUTBOUND` | the exact packed envelope bytes, as handed to the transport |
| `0x03` | `INBOUND` | the exact received envelope bytes, as verified |
| `0x04` | `HAND_CLOSE` | the hand outcome record |
| `0x05` | `TERMINAL` | the `TerminalRecord`, for an **evidence-derived** terminal only (§8.5) |
| `0x06` | `LOBBY` | the lobby record of §6.7 |

An unknown `type` is malformed (§6.3), not skippable. Forward compatibility
is the `journal_version`'s job, not a per-record guess: a reader that skipped
a record it did not understand would resume from a state it could not
reconstruct while believing it had.

**Version handling is fail-closed and asymmetric.** A journal whose magic
does not match, or whose `journal_version` this build does not implement,
MUST NOT be parsed, truncated, repaired, or resumed from. The peer refuses to
resume and says which version it found. Writing a new format over an
unreadable old one is the `identity.py` failure of §5.4 with a larger blast
radius.

### 6.3 Torn writes, and the sharper case of corruption

On open, records are read forward. A record is **framing-valid** only if its
declared length is within the bounds of §6.2, fits in the remaining file, and
its digest matches.

**A framing-invalid record at the end of the file is a torn tail.** The
journal MUST be truncated to the end of the last framing-valid record and
recovery MUST proceed from there. A partially written record MUST NOT be
partially interpreted, and MUST NOT be repaired.

Truncation is safe precisely because of §6.1: a lost tail record was, at worst,
sent-but-unrecorded — and by the ordering rule that state is unreachable.

**A framing-invalid record with framing-valid records after it is not a torn
tail.** An append-only file fsynced record-by-record cannot produce that
shape from an interrupted write; it means the file was damaged or edited.
Truncating there would silently discard durable history that a peer has
already acted on — including `author_seq` numbers other peers hold. The peer
MUST refuse to resume and MUST NOT truncate.

**Framing-valid but semantically malformed is also a refusal, not a
truncation.** A record whose digest matches but whose payload violates §6.7
or §6.8 — a bad payload version, a length that does not consume the payload
exactly, a value outside its stated bounds, a `seats_in` that is not strictly
increasing, a boolean that is neither `0x00` nor `0x01` — is corruption of
recorded history, not an interrupted write. The peer MUST refuse to resume,
name the record index and the field, and change nothing on disk.

The distinction matters because the two failures want opposite handling.
Truncating a torn tail loses nothing that was ever sent; truncating a damaged
record loses history that was.

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
witness, and no proof randomness. Everything in that list is re-derived from
`device_secret.py`'s existing file (§5.3).

**Exactly one non-public value is written: the admission secret, in the
`LOBBY` record (§6.7).** It is named here rather than left to be discovered in
the record layout, because "the journal stores nothing secret" would be the
more comfortable sentence and it would be false.

What it is, precisely: a 16-byte capability from the invite, proving *"holds
the invitation"* and nothing else (`admission.py:38-44`). It is not a signing
key, and it confers no seat — `_on_player_info` is LOBBY-only
(`session.py:2415-2420`) and `_bind_seat_keys` is one-way
(`session.py:1674-1675`), so a mid-hand holder of the secret can complete
admission and still obtain no seat, no share, and no authority over any
message. It is already shared with every invited player and travels as a
copy-pasteable room code.

Why it is written anyway: without it a restarted peer cannot re-authenticate,
and failure class 2 collapses into class 3 for **every** role — the joiner
cannot answer a challenge, and the host cannot issue one that any existing
invite satisfies (§9.7). The alternative is §6.7's re-supply path.

The bound: an attacker who can read this file can also read
`device_secret.py`'s master secret and `identity.py`'s private key, and with
those two can simply *be* this peer. The capability adds nothing to what that
attacker already holds, so the journal's sensitivity remains the threat model
`device_secret.py:16-33` states and accepts: play money, no custody, and an
attacker with the config directory can already read process memory. **Do not
reuse this design for anything of value without revisiting that decision.**

### 6.6 Retention

A journal is scoped to a session and MAY be discarded once the session reaches
a terminal state and its `TERMINAL` record is durable. It MUST NOT be discarded
while a hand is `SUSPENDED` — including after a local stand-down (§8.4, E3),
which is precisely when discarding it would convert a recoverable interruption
into a permanent one.

### 6.7 The `LOBBY` record

Written once, when the table's invite parameters are established: on the host
when it generates the room code, on a joiner when it parses one. It carries
what that role needs to run the admission handshake of §9.2 step 2 after a
restart.

```
lobby := payload_version : u32be           # 1
      ‖ role             : u8              # 0x01 host, 0x02 joiner
      ‖ host_pubkey      : 32 octets       # the exact pinned host key
      ‖ discovery_token  : 8 octets
      ‖ admission_secret : 16 octets
      ‖ listen_port      : u16be           # host: the bound port. joiner: 0
      ‖ host_addr_len    : u16be           # 0 ≤ len ≤ 255
      ‖ host_addr        : host_addr_len octets, UTF-8   # joiner: "host:port"
```

Field widths mirror `invite.py`'s payload exactly (`HOST_PUBKEY_LEN`,
`DISCOVERY_TOKEN_LEN`, `ADMISSION_SECRET_LEN`) so a mismatch is a length
error rather than a silent reinterpretation. A `role` other than `0x01` or
`0x02` is malformed, as is a payload not consumed exactly, as is a
`host_addr_len` above 255 (§6.3).

`host_addr` is the joiner's dial target and MAY be empty for a host, which
binds rather than dials. It is a **hint**, not authority: a joiner that
reaches the recorded address still pins the host key at §9.2 step 3, so a
stale or poisoned address costs a failed connection and nothing else.

**A second `LOBBY` record for a different tuple is malformed.** Invite
parameters are per-table and write-once, like the deal policy. A journal
holding two of them cannot say which lobby it belongs to.

**Permitted alternative, and the only one.** An implementation MAY omit the
`LOBBY` record and instead require the returning user to re-enter the room
code. It MUST NOT proceed without the parameters by any other route — no
regeneration, no default, no zero-filled secret. The choice is local: these
parameters are inputs to *this peer's* authentication, never replicated state,
so the two options differ in user experience and in what sits on disk, and not
at all in what any other replica observes.

### 6.8 `HAND_OPEN`, canonically

`HAND_OPEN` is the pre-hand revert point and the seat's own record of the
domain it is playing in. Deterministic class-2 recovery is exactly as good as
this encoding is unambiguous, so it is specified to the octet.

```
hand_open := payload_version : u32be       # 1
          ‖ ctx_len          : u32be
          ‖ ctx_preimage     : ctx_len octets     # §3.2, verbatim
          ‖ hand_no          : u64be
          ‖ seat_count       : u16be
          ‖ local_seat       : u16be
          ‖ button           : u16be
          ‖ seats_in_count   : u16be
          ‖ seats_in         : seats_in_count × u16be
          ‖ prevention       : u8                 # 0x00 | 0x01
          ‖ stacks           : seat_count × u64be
          ‖ names            : seat_count × (u16be len ‖ len octets UTF-8)
          ‖ sb               : u64be
          ‖ bb               : u64be
          ‖ structure_len    : u16be
          ‖ structure        : structure_len octets, UTF-8
          ‖ has_positions    : u8                 # 0x00 | 0x01
          ‖ positions        : present iff has_positions == 0x01:
                               bb_seat : u16be ‖ sb_seat : u16be ‖ btn : u16be
```

Every integer is **unsigned** big-endian; there is no negative quantity in
this record. Chips are integers throughout the engine (`ReplicaTable`
constructs `Player(i, names[i], stacks[i])` from ints), so a float stack is a
refusal, never a rounding.

**Field order is parse order.** `seat_count` precedes every field bounded by
it, so a reader validates in one forward pass and never has to hold an
unvalidated count. Field order is normative for the same reason it is in §3.2:
a different order is a different format and needs a different
`payload_version`.

**Bounds, each of which is a refusal when violated (§6.3):**

| Field | Bound | Why |
|---|---|---|
| `payload_version` | exactly `1` | any other value is a version this build does not implement |
| `ctx_len` | 1 ≤ `ctx_len` ≤ 65 535, and the pre-image MUST parse under §3.2 with its own `seat_count` equal to this record's | the context is the authority for the seat → key vector; an unparsed blob would let a resume authorize against bytes nobody validated |
| `seat_count` | 2 ≤ `n` ≤ 23 | the deal consumes `2n + 5` deck positions (`deal_map.py:96-122`) and the deck is 52, so 23 is the arithmetic ceiling. A stricter product limit is permitted; a looser one is not |
| `local_seat`, `button` | `< seat_count` | `button` need not be in `seats_in` — the dead-button rule vacates it (`deal_map.py:72-79`) — but it must name a seat that exists |
| `seats_in` | strictly increasing, every entry `< seat_count`, `2 ≤ seats_in_count ≤ seat_count`, and `local_seat ∈ seats_in` | strict increase makes the encoding canonical: one membership set has exactly one byte string, so two peers cannot journal the same hand differently. `begin_hand` already refuses a local seat that is not dealt in (`session.py:1128-1130`) |
| `stacks`, `names` | exactly `seat_count` entries each | `ReplicaTable.__init__` raises when they do not align (`replica_table.py:73-74`); catching it here names the file instead of the constructor |
| name length | ≤ 64 octets | a display string, bounded so a journal cannot be grown through it |
| `structure_len` | 1 ≤ len ≤ 32 | `"No-Limit"` and its siblings |
| `sb`, `bb` | `0 < sb ≤ bb` | a zero or inverted blind level is not a table this protocol dealt |
| `positions` seats | each `< seat_count` | the dead-button chain names seats, and a vacated one is legal |
| trailing octets | none | the payload MUST be consumed exactly. A trailing byte means the writer and the reader disagree about the layout, which is the failure this section exists to make impossible |

**`button` is the post-advance value.** `_begin_p2p_hand` passes
`self._replica.button` — read *after* `start_hand` moved it — into
`begin_hand` (`session.py:2019-2020`), and that is the value the deal map and
therefore hole-card ownership is computed from. `_table_cfg["button"]` is the
pre-move value and is **not** this field. Journalling the wrong one is the
card-leak of §5.1 arriving through the recovery mechanism, so the two are
distinguished here rather than left to a reader of the code.

`has_positions == 0x00` is the first hand of a session and a first-hand
redeal, where `_hand_positions` is `None` (`session.py:1975-1980`). It is a
real state, not a missing value, and it MUST NOT be encoded as a triple of
zeros — seat 0 is a legal button.

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
`player_list`, `game_start`, chat, the admin messages, the resume handshake of
§9, and `seat_lost` (§8.6). Each of these is sent live by a live peer, so a
clock window costs them nothing; `seat_lost` is in this class for the further
reason given in §8.6, that its sender may have no journal to draw an
`author_seq` from.

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

A restarted process enters `SUSPENDED` directly, without observing a
disconnect at all: it reconstructs an open hand from its journal and every
remote seat is, by definition, not yet connected. For a restarted host that
means every other seat (§9.7.2 step 3).

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

| Exit | Trigger | Kind | Chip effect |
|---|---|---|---|
| **E1 · RESUME** | the absent participant completes §9 and its outstanding contributions arrive | replicated, evidence-driven | none — committed chips stay committed, uncommitted stacks stay with their owner |
| **E2 · UNRECOVERABLE** | authenticated evidence that recovery is impossible: a valid `seat_lost` declaration (§8.6), or this peer's own proven loss | replicated, evidence-driven | **none.** Chips frozen exactly as committed. `BLOCKED / UNRECOVERABLE` |
| **E3 · LOCAL STAND-DOWN** | this process stops waiting: a local grace deadline, the user abandoning the hand, or shutdown | **local only.** Not a session outcome, not absorbing, not evidence | none. Chips frozen as committed; the hand stays `SUSPENDED` on disk |
| **E4 · INTEGRITY FAILURE** | signed evidence of a cryptographic failure — equivocation, a failed proof, a failed audit — that would have voided the hand with every peer present | replicated, evidence-driven | the existing `VOID_*` rules apply, including the stack revert |

**E4 is narrow on purpose, and the boundary is the discriminating invariant of
this whole section.** It is permitted only when the trigger is *signed evidence
of misbehaviour* whose verdict does not depend on anyone being absent. Absence
itself, a missing contribution, an elapsed deadline, and a failure to
retransmit are **not** E4 triggers and MUST NOT reach a void. Standing
invariant 5 governs the other direction too: a cryptographic integrity failure
is not a casino misdeal, and "play on" is not available for it.

There is no fifth exit. In particular there is **no** sole-live-player
settlement exit; see §11.

### 8.5 E3 is local, and that is the whole point

An earlier draft of this contract let a local grace deadline drive E2 —
a private, unbound, untransmitted timer producing an absorbing session state.
Independent review rejected it, correctly: two replicas holding **identical
signed evidence** could then occupy incompatible absorbing states, one
terminated while the other stayed suspended and later resumed, and a
five-second Wi-Fi drop could be made permanently unrecoverable by whichever
replica happened to have the shortest timer. Standing invariant 3 does not
have an exception for transitions that allocate no value.

So E3 creates no shared state at all. Precisely:

* it MUST NOT write a `TERMINAL` journal record, and MUST NOT be recorded as
  a session outcome or a hand outcome;
* it MUST NOT be broadcast, and no message asserting it exists. A peer that
  has stood down is, to every other replica, exactly a peer that is absent —
  which is what it is;
* it MUST NOT delete the journal (§6.6). The hand remains `SUSPENDED` on
  disk;
* it is **not absorbing**. A later start of the same process reads the same
  journal, re-enters `SUSPENDED`, and may resume through §9;
* it MAY stop the process, close the table in the UI, and tell the user the
  hand cannot continue here right now. That is presentation, and presentation
  is where a local clock belongs.

**Elapsed local time MUST NOT gate whether a resume is accepted.** A replica
still running accepts a valid `seat_resume` (§9) for a `SUSPENDED` hand no
matter how long the seat has been away. There is no expiry on returning,
because an expiry would be a private timer deciding a replicated outcome
through the back door.

The grace period survives only as **local policy for presentation and process
lifetime**. It is not bound into the deal context, not transmitted, and not
evidence. M2 blesses no value for it.

### 8.6 `BLOCKED / UNRECOVERABLE` and the `seat_lost` declaration

A terminal session state, reached by **E2 only**. Chips are frozen at their
committed position: no winner, no refund, no redeal. The `TerminalRecord`
(`session.py:2787-2799`) records the cause and the seat whose participation
was lost, and a `TERMINAL` journal record makes it absorbing across restarts.

Two triggers, and no others.

**(a) A `seat_lost` declaration.** New, table-wide, signed,
`FRESHNESS_STRICT`. It carries `ctx`, `hand`, `seat`, a 16-byte nonce, and a
reason code. A receiver accepts it only when the envelope's author
byte-equals `_seat_keys[seat]`, `ctx` byte-equals its own, and `hand` equals
the suspended hand. Every replica that receives it makes the identical
transition from the identical bytes, with no clock involved — which is what
makes E2 replicated rather than private.

It is emitted by a peer that can still authenticate for its seat but cannot
resume it: the device secret is gone, the journal is unreadable (§6.3), or
the user has decided the seat will not return and is willing to say so on the
record. Note what that costs the sender: it is the *only* honest way to end
the hand for everyone, and it ends it with the sender's own chips frozen too.

**It must be relayed.** In the star topology a joiner's broadcast reaches the
host and no one else (`TOPOLOGY_DECISION.md` §1), so the host MUST forward
`seat_lost` to every other peer **byte-for-byte**, as it forwards the hostless
set — courier, never re-signer. Without that, one replica terminates and the
rest wait forever on a declaration they were never shown, which is precisely
the divergence this exit exists to remove. The host cannot forge it
(signatures) and can only suppress it, which leaves every suppressed replica
`SUSPENDED` with frozen chips: the safe direction.

**It is deliberately not `author_seq`-numbered**, and this is not an
oversight. A peer whose journal is unreadable cannot derive its next outbound
`author_seq` (§7.6). Numbering the declaration would make it re-issue a
number peers have already bound to a different fingerprint — equivocation,
read as E4, ending in a `VOID_*` that reverts stacks. The one message a lost
peer must be able to send would then be the message that refunds it. So
`seat_lost` sits outside the sequenced stream, and takes the strict clock
window like the rest of the unsequenced types (§7.5).

**It grants no new power.** A malicious seat can send `seat_lost` and freeze
the table — but it can achieve exactly that today by simply never
contributing again, because the deal is n-of-n (**B1**). The declaration
makes an existing, unavoidable freeze immediate and attributable instead of
indefinite and anonymous.

**(b) This peer's own proven loss.** A peer that restarts and finds its
device secret absent, its journal at an unreadable version, or its
`identity.json` refusing to load (§5.4) has *local* proof that it cannot
resume its seat. It records `BLOCKED / UNRECOVERABLE` for itself, and MUST
emit `seat_lost` if it still holds the seat key to sign with. Where it
cannot sign, no evidence can reach the table and the other replicas stay
`SUSPENDED` — see the limitation below.

**What has no trigger, and why that is the honest answer.** Total device loss
produces no evidence and never will: the peer that could sign the declaration
is precisely the peer that is gone. Those replicas stay `SUSPENDED`
indefinitely, holding frozen chips, until each user stands down locally (E3).
The chip position is identical to `BLOCKED / UNRECOVERABLE` in every respect;
what is missing is only the ability to *say* the loss is permanent. Inventing
a timer to say it anyway is the finding this section was rewritten to fix.

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

For a class-2 return the peer must additionally hold the invite parameters
(§6.7): admission is a fresh handshake, not a resumed one, and a peer that
cannot answer a challenge cannot reach step 4 whatever its seat key proves.
A class-1 return still holds them in memory, which is why the requirement is
invisible until a process restarts.

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

**Two refusals, both deterministic, neither time-based.**

* A `seat_resume` for a session that has reached an **evidence-derived**
  terminal (§8.6) is refused, and the refusal carries the `TerminalRecord` so
  the returning peer learns the cause rather than retrying into silence. Every
  replica refuses identically, because every replica reached that terminal
  from the same evidence.
* A `seat_resume` for a hand that is merely `SUSPENDED` is accepted regardless
  of how long the seat has been away (§8.5). A host MUST NOT expire an absent
  seat, and MUST NOT make acceptance conditional on a local clock.

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
terminating immediately. A joiner that gives up on the wait performs a **local
stand-down** (E3): it may stop, and it may show the user `HOST_LOST`, but it
writes no `TERMINAL` record and asserts no session outcome — because a host
that has restarted is coming back (§9.7), and a private timer must not be able
to decide that it has not. Terminal semantics, when a terminal is actually
reached, are unchanged.

`HOST_LOST` **as a durable terminal survives in LOBBY only**, where it is
already correct for a reason that has nothing to do with waiting: wire mode
refuses to elect a replacement because the invite pins one host key
(`session.py:2903-2911`), no hand exists, and no chips are committed.

The safety argument, precisely: migration is forbidden because a **different**
peer would inherit host authority and every joiner's pin would be wrong.
Resumption grants authority to the **same pinned key**, and
`mark_host_authenticated` already refuses anything else, comparing all 32 bytes
(`session.py:1336-1353`). Chips are frozen under either behaviour, so this
changes no chip outcome; it only preserves the option of resuming. Host
migration remains forbidden and is not reopened.

The tests that pin the current immediate-termination behaviour
(`tests/test_host_loss.py`) must be revisited by the implementation, not
bypassed: the PLAYING case changes deliberately, and the LOBBY case must not.

### 9.7 Host process restart

A host is a seat with a listener attached. Failure class 2 therefore applies
to it unchanged, and this section says how — the earlier draft named host
restart a known limitation, which independent review rejected as
incompatible with the class-2 outcome §1 claims.

Nothing here grants the returning host any authority it did not have. It
returns as **the same pinned key**, which is the same test
`mark_host_authenticated` already applies to every joiner's invite
(`session.py:1336-1353`). A host that cannot present that key is not a host;
it is a stranger, and §9.2 step 3 refuses it.

**9.7.1 What the host must recover, and from where.**

| Needs | Source | Note |
|---|---|---|
| host signing key | `identity.json` | the key every joiner's invite pins. §5.4 is what makes this dependable |
| `master_secret` | `device_secret.py` | the host's own share input |
| seat → key vector, `hand_no`, `button`, `seats_in`, stacks, table config | `HAND_OPEN` (§6.8) | identical to any other peer's recovery |
| envelopes, `author_seq` | `OUTBOUND` / `INBOUND` records | §7.4, §7.6 |
| `admission_secret`, `discovery_token` | `LOBBY` (§6.7) | **without these no existing invite can be honoured**, and every joiner is locked out of a table it is legitimately seated at |
| bound listen port | `LOBBY` | the invite carries an address; a new port makes the old code unreachable |

**9.7.2 Sequence.**

1. **Journal first, socket second.** The host reads its journal, refuses on
   any §6.3 refusal condition, and reconstructs `HAND_OPEN` state and its
   own `author_seq` position. It MUST NOT listen before this completes: a
   joiner admitted by a half-built host could be answered from state that
   later fails to parse.
2. **Terminal check.** If a `TERMINAL` record is present the session is over
   and stays over. The host MAY listen solely to answer with that record,
   and MUST NOT resume a hand.
3. **Enter `SUSPENDED`,** with *every* remote seat absent. This is the
   ordinary `SUSPENDED` of §8, not a special case: no chips move, no timer
   converts silence into an action, and the local seat may not act.
4. **Restore the listener** on the port from the `LOBBY` record, and
   reconstruct `HostAdmission` from the recorded `admission_secret`,
   `discovery_token` and its own public key. Admission state itself is
   *not* restored — it is connection-scoped and every connection is new
   (`session.py:2841-2842`), which is exactly the property that makes a
   captured response from before the restart worthless.
5. **Rebuild the hop table** as placeholders (§5.2), and restore `_seat_keys`
   **from the recovered context pre-image**, before anything can call
   `_bind_seat_keys`. This applies to every recovering peer, not only the
   host, and the ordering is the point: `_bind_seat_keys` derives the map
   from the *roster*, and a restarted process has an empty roster, so
   letting it run first would freeze an empty map in compat or raise in wire
   mode (`session.py:1692-1709`). Restoring first makes it a no-op
   (`session.py:1674-1675`) and keeps the binding one-way, which is what
   §2.5 depends on. `local_conn_id` is whatever the new transport mints.
6. **Every joiner reconnects through §9.2 unchanged** — full re-admission
   against a fresh server nonce, host pin verified against the invite, then
   `seat_resume` authorized against the frozen seat key. The host answers
   with `seat_resume_ack` and repoints that seat's hop. Nothing in §9.2
   needed a host that had been running continuously; it needed a host that
   holds the pinned key and the frozen binding, and both survived.
7. **Retransmission and replay** proceed as in §9.2 steps 9-10, in both
   directions: the host re-emits its journalled `OUTBOUND` records for
   seats that lack them, and requests those it lacks.
8. **Exit.** Each replica leaves `SUSPENDED` via **E1** as the outstanding
   contributions arrive. Nothing about the exit is host-specific: the host's
   own return supplies its contributions the same way any seat's does.

**9.7.3 Joiners must not be required to notice.** A joiner cannot distinguish
a host that dropped from a host that restarted, and MUST NOT try. Both are
"the host connection went down" (§9.6): suspend, retry the invite address,
re-authenticate on success. This is deliberate — a protocol that behaved
differently for the two cases would need to decide which had happened from
the outside, which is exactly the judgement no peer can make.

**9.7.4 What is still lost, and what that costs.** The `LOBBY` record fixes
identity and capability, not reachability. If the host's address or NAT
mapping changed while it was down, joiners holding the old code cannot reach
it, and the host must issue a regenerated invite carrying the **same**
`discovery_token` and `admission_secret` — which `invite.generate_room_code`
already supports for exactly this reason (`invite.py:114-120`) — and get it to
the players out of band. That is a **liveness** cost, not a safety one:
nothing resumes, nothing settles, no chip moves, and every peer holds the
same frozen position it held while suspended.

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
   not revert to `_hand_stacks`, and declares no winner (§8.6).
4. Reconnect restores state by replay and re-derivation only; no recovery path
   writes a stack value (§7.3).
5. There is no sole-live-player settlement exit (§11).
6. A local stand-down moves nothing either: it writes no outcome and reverts
   no stack, and the position it leaves on disk is the suspended one (§8.5).

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
| C23 | an unknown journal version is refused, never rewritten | parse a journal whose `journal_version` this build does not implement, or overwrite it with a fresh header | history from another version is resumed from, or destroyed |
| C24 | corruption is a refusal; only a torn **tail** truncates | truncate at the first framing-invalid record even when framing-valid records follow it | durable history — including `author_seq` numbers peers hold — is silently discarded |
| C25 | `HAND_OPEN` has one canonical encoding | encode `seats_in` unsorted, omit `has_positions`, widen a field, or leave a trailing octet | a pinned `HAND_OPEN` byte vector, plus a malformed-payload refusal control per §6.8 bound |
| C26 | the journalled `button` is the post-advance value | journal `_table_cfg["button"]` instead of `_replica.button` | the recovering seat broadcasts a decryption share for its own hole position |
| C27 | a local stand-down is not a terminal | write a `TERMINAL` record (or delete the journal) when the grace deadline elapses | a restarted peer refuses a resume that must succeed, or has no journal to resume from |
| C28 | resume acceptance is never time-gated | expire an absent seat after the grace period | a late but otherwise valid `seat_resume` is refused |
| C29 | E2 requires evidence | enter `BLOCKED / UNRECOVERABLE` on the local grace deadline | two replicas holding identical signed evidence reach different terminal states |
| C30 | `seat_lost` is authenticated and context-bound | accept it without the `_seat_keys[seat]` author check, or without the `ctx`/`hand` comparison | a stranger, or another table's declaration, terminates a healthy hand |
| C31 | `seat_lost` is outside the sequenced stream | stamp it with an `author_seq` | a journal-less peer's declaration is read as equivocation and the resulting void refunds it |
| C32 | a host process restart resumes at the exact seat | regenerate the admission secret at startup instead of reading the `LOBBY` record | every joiner is locked out of a table it is legitimately seated at. Paired with a positive control: restart the host, reconnect both joiners, complete the hand |
| C33 | a placeholder hop authorizes nothing | rebuild `_seat_order` with a placeholder equal to `local_conn_id` | an absent seat's message is authorized by the self-delivery shortcut (`session.py:1759-1760`) |
| C34 | `seat_lost` reaches every replica | leave it out of the host's relay set | at three peers, one replica terminates while another stays suspended holding the same evidence set |
| C35 | seat keys are restored before anything rebinds them | let `_bind_seat_keys` run on a recovered session before the journalled map is restored | wire mode raises on the empty post-restart roster, or compat freezes an empty map and every seat becomes unauthorizable |

---

## 13. Known limitations

* **Nothing in this document has been executed.** No test was run and no
  digest was computed on the host that wrote it. §3.5 pins bytes, which are
  checkable by inspection; it deliberately pins no SHA-256.
* **Host process restart is specified (§9.7) and unimplemented.** So is
  everything else here; it is called out because an earlier draft of this
  document carried it as *unsolvable*, which review showed was a description
  of the shipped code rather than of the design. What it needs is listed in
  §9.7.1, and C32 is its control.
* **Host reachability after a restart is not guaranteed** (§9.7.4). A changed
  address or NAT mapping leaves joiners unable to reach a host that is
  otherwise perfectly recoverable, and the remedy — a regenerated invite
  carrying the same token and secret — is out of band. Liveness only: no chip
  moves either way.
* **Permanent participant loss is not always *provable*** (§8.6). Where the
  lost peer cannot sign a `seat_lost` declaration, the surviving replicas hold
  frozen chips in `SUSPENDED` rather than in `BLOCKED / UNRECOVERABLE`. The
  chip position is identical; the difference is that nothing can say the loss
  is permanent, and M2 deliberately declines to let a timer say it.
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
