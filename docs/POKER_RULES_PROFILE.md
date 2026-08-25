# Professional Poker Rules Profile

**Profile identifier:** `poker.tda.2024.nlhe.v1`
**Type:** normative contract (see `docs/COLLABORATION.md`, "Research versus
contract"). Implementation that contradicts this document is a bug in the
implementation.
**Milestone:** M1. **Base:** `c69a4bf` (`main`).
**Research input:** `docs/research/professional-poker-rules-audit.md`.

This document classifies the professional rules this game adopts, digitalizes,
overrides, or discards, and records the rules decisions that had no home
before it existed.

Two things it deliberately does **not** do, per `docs/ROADMAP.md`:

* It changes no engine behaviour. Conformance is **M7**; §7 is the gap list
  M7 works from.
* It does not bind the identifier into the live deal context. `_deal_context_bytes`
  is untouched; the binding rides **M2**'s single deliberate B7 domain and
  version break, so the wire context is redesigned once (ROADMAP Decisions,
  2026-08-18).

---

## 1. The profile identifier, frozen

### 1.1 The string

```
poker.tda.2024.nlhe.v1
```

Grammar: `<game>.<authority>.<edition>.<variant>.v<n>`, where

| Field | Value | Meaning |
|---|---|---|
| game | `poker` | the family |
| authority | `tda` | Poker Tournament Directors Association |
| edition | `2024` | the **published** ruleset edition this profile reads |
| variant | `nlhe` | No-Limit Texas Hold'em |
| revision | `v1` | this profile's own revision of that reading |

The character set is `[a-z0-9.]` only: ASCII lowercase letters, ASCII digits,
and the `.` separator. No uppercase, no underscores, no hyphens, no Unicode.

### 1.2 The canonical encoding

The identifier is a **byte string**, not a parsed structure. It is compared
byte-for-byte. No implementation may case-fold it, trim it, apply Unicode
normalisation to it, re-order its fields, or split it on `.` before comparing.
A peer that does not hold these exact bytes does not hold this profile.

```
identifier    poker.tda.2024.nlhe.v1
encoding      UTF-8 (identical to ASCII for this string)
length        22 octets
octets        70 6f 6b 65 72 2e 74 64 61 2e 32 30 32 34 2e
              6e 6c 68 65 2e 76 31
```

Wherever the identifier enters a hashed pre-image it is carried in exactly one
form: **a 4-octet big-endian length followed by the UTF-8 octets**, which is
the length-prefixed idiom `Session._deal_context_bytes` already uses for the
deal policy and for every seat id (`holdem/p2p/session.py:891-904`).

```
encoded form  00 00 00 16 | 70 6f 6b 65 72 2e 74 64 61 2e 32 30 32 34 2e
                            6e 6c 68 65 2e 76 31
```

The length prefix is not decoration. The pre-image it will join is injective
precisely because every variable-length field is length-prefixed; a bare
concatenation of two profile-adjacent strings is not (`session.py:859-886`
records what a non-injective encoding cost the first time).

### 1.3 What "frozen" means

Frozen means **this string and this encoding are now the definition**. They may
not be edited in place. A change to either is a new identifier *and* a deal-context
layout version bump (`_DEAL_CTX_VERSION`, `session.py:857`, currently `2`; M2
takes it to `3`).

| Change | Consequence |
|---|---|
| TDA publishes a 2026 ruleset and we adopt it | new identifier `poker.tda.2026.nlhe.v1`; not before TDA publishes it |
| a ruling here changes how a hand plays | revision bump, e.g. `poker.tda.2024.nlhe.v2` |
| wording, citations, evidence links only | no bump; the identifier describes rulings, not prose |
| the encoding or the field's position in the pre-image changes | new identifier **and** a domain version bump |

TDA Summit XII met 29–30 June 2026 and no 2026 ruleset is published, so `2024`
is the current published edition (research note, "Source hierarchy and
versions"). Adopting a 2026 identifier before TDA publishes one would pin this
protocol to a document that does not exist.

### 1.4 Where it will be bound (M2, informative)

M1 specifies the value and its encoding; M2 places it. What M1 requires of M2:
the identifier is bound as **one** length-prefixed UTF-8 field in the versioned
deal-context pre-image, alongside the deal policy and seat order already bound.
Its position within that pre-image is M2's to fix, because the same break also
replaces `conn_id` with a stable seat identity (B7). What M1 forbids: splitting
the identifier across fields, storing a hash of it in place of the string, or
transmitting a parsed form.

Until that binding lands, two peers can start a hand under different readings
of these rules without the protocol noticing. That is a known, accepted,
scheduled gap, not an oversight — see §9.

---

## 2. Sources and citation provenance

| Source | Version | Role |
|---|---|---|
| Poker TDA Rules | **2024** | primary normative base |
| WSOP | 2026 Main Event | cross-check, clock policy |
| GGPoker house rules | current | cross-check, online disconnect/timeout |

Every rule citation below carries a provenance marker, so a reviewer can tell
what is verifiable inside this repository from what must be checked against the
published TDA text:

| Marker | Meaning |
|---|---|
| **[V]** | quoted verbatim in `docs/research/professional-poker-rules-audit.md` |
| **[R]** | rule **number and substance** recorded in that research note |
| **[U]** | cited by title/substance only — the rule **number is not established in this repository** and must be confirmed against the published TDA 2024 text before anyone quotes it as a number |

Nothing here invents a rule number. Where the number is unverified the row says
so rather than guessing.

---

## 3. Classification matrix

**ADOPT** — the professional rule is taken as written; the digital form is the
same rule.
**DIGITALIZE** — the *consequence* is adopted, the *mechanism* is replaced,
because the mechanism assumes a human floor, a dealer, or physical objects.
**OVERRIDE** — this game deliberately does something the professional rule does
not say, and the reason is recorded.
**N/A** — the rule exists to manage a physical or procedural failure mode that
cannot occur here.

### 3.1 Cards, settlement, showdown

| Area | TDA 2024 | Class | Ruling | Evidence |
|---|---|---|---|---|
| Cards speak | 12 [R] | ADOPT | the verified cards plus the evaluator determine the result; no UI text, chat, or declaration alters settlement | `engine.py:910-915` scores from `evaluate(p.hole + b)` |
| All-in hands tabled | 16 [R] | ADOPT | an all-in showdown is tabled, not mucked | `engine.py:1000-1002` `must_show` is true whenever `tabled` |
| Showdown order | 17 [R] | ADOPT | last river aggressor tables first; otherwise the first live seat left of the button | `engine.py:984-995` |
| Beaten hand may muck | — [U] | **OVERRIDE** | in the P2P game every contested seat is tabled at showdown; there is no muck right. The engine's muck path still exists and still runs in the single-process game, where the deck is not opened | `session.py:2354-2355` passes `force_tabled=True` for every P2P showdown; `engine.py:1000-1019`; §4 |
| Odd chips | 20 [R] | ADOPT | to the first winner left of the button, walking forward. **Consensus-critical** — every replica must award the same chip | `engine.py:969-977` |
| Uncalled bet returned | — [U] | ADOPT | returned only to a single seat holding the top live total | `engine.py:861-877` |
| Dead money never returns | 30 [R] | ADOPT | antes and dead blinds stay in the pot | `engine.py:861-863`, `settle` never refunds `total_dead` |
| Side pots | — [U] | ADOPT | layered by committed total, identical-eligibility layers merged, residual dead money to the top pot | `engine.py:917-938` |
| Run it twice | — | **OVERRIDE** | not a TDA tournament rule; supported as a cash-game extension in the single-process engine (odd chip to run 1), and **structurally impossible** in the P2P game — a second run deals from the deck, which mental poker cannot produce | `engine.py:884-898`, `engine.py:947`; `replica_table.py:29-31` pins `runs=1` |

### 3.2 Position, blinds, deal

| Area | TDA 2024 | Class | Ruling | Evidence |
|---|---|---|---|---|
| Dead / moving button | 34 [R] | ADOPT | forward-moving BB anchor; SB and button trail it and may land on vacated seats | `engine.py:462-506` |
| Heads-up button | 34-B [R] | ADOPT | SB is the button, dealt last, acts first pre-flop and last thereafter | `engine.py:471-473`, `engine.py:613-617`, `engine.py:830` |
| Big-blind ante order | — [U] | ADOPT | the ante is posted after the blind | `engine.py:569-571` |
| Burn cards | — | **N/A** | no burn cards exist: the deal map is `2m + 5` positions and nothing else | `deal_map.py:13`, `engine.py:814-825` |
| Straddle | — [U] | **unverified** | the engine supports a UTG straddle; it has **not** been checked against TDA straddle provisions. Carried, not claimed | `engine.py:585-600` |

### 3.3 Betting

| Area | TDA 2024 | Class | Ruling | Evidence |
|---|---|---|---|---|
| No maximum bet (NLHE) | — | ADOPT | bounded only by stack | `engine.py:664-705` |
| Raise amounts | 43 [R] | DIGITALIZE | the "50% or more" clause is a remedy for an ambiguous human gesture. There is no gesture: a sub-minimum raise is **rejected**, never rounded up | current code coerces — gap **C2**, §7 |
| Re-opening the bet | 47 [R] | ADOPT | including **cumulative** short all-ins that together total a full raise | current code judges each all-in alone — gap **B6**, §7 |
| Action out of turn | 53 [R] | DIGITALIZE | rejected before it becomes game state. No backup, no binding, no penalty machinery — those exist to unwind an action that was already physically taken | `replica_table.py:176-177` rejects a non-actor; engine-level guard is gap **C3** |
| Binding declarations, undercalls | 51 [R] | **N/A** | one canonical typed action, validated once, accepted once | `client_view.py:244-251` |
| Oversized chip betting | 44 [R] | **N/A** | no chips are pushed |
| String bets, verbal-vs-chip conflict, dealer gesture | various [R] | **N/A** | the whole class is eliminated by typed actions |

### 3.4 Clock and absence

| Area | TDA 2024 | Class | Ruling | Evidence |
|---|---|---|---|---|
| Calling for a clock | 29 **[V]** | DIGITALIZE | *"A player on the clock has up to 25 seconds plus a 5 second countdown to act. If the player faces a bet and time expires, the hand is dead; if not facing a bet, the hand is checked."* The **consequence** is adopted verbatim, including the fold/check split; the 25 + 5 becomes a single deterministic `T = 30 s`; the **authority** to declare expiry is replaced, because there is no floor to call | `TIMEOUT_SPEC.md:69-79` (betting default 30 s); the corrected contract is **M3** |
| Absent player, blinds forfeit | 30 **[V]** | ADOPT | cards killed, posted blinds forfeit to the pot. This is the professional precedent for the non-profitability invariant: absence forfeits the hand and the chips already committed, and does not refund them | ROADMAP standing invariants 1 and 4 |
| Timeout while **cryptographically** absent | — | **OVERRIDE** | no professional analogue exists. A seat may stop making poker decisions and still be a required cryptographic participant; that case is suspension semantics, **M2/M3**, not a poker rule | ROADMAP standing invariant 2, blocker B1 |
| Clock duration as event policy | WSOP 2026 [R] | — | WSOP ran a 20 s clock with extension chips on Day 7 and removed it for the final table, confirming duration is event-level policy rather than a fixed rule. `T` is therefore a table parameter, and a **consensus-critical** one (§8) | research note, "Source hierarchy" |

### 3.5 Errors, integrity, ethics

| Area | TDA 2024 | Class | Ruling | Evidence |
|---|---|---|---|---|
| Misdeals, fouled decks | 35 [R] | **N/A** | there is no physical dealing. Explicitly **not** the home for cryptographic failures — see §6 |
| Substantial action | 36 [R] | **N/A** | exists to bound the correction of physical errors; "substantial action occurred, play on" must never be applied to an integrity failure — §6 |
| Exposed cards, fouled deck, dropped cards | various [R] | **N/A** | eliminated by a cryptographic deck |
| Ethical play — soft play, chip dumping, collusion | Ethical Play [U] | **OVERRIDE (policy, not invariant)** | these are real risks and this protocol **cannot** enforce them. TDA's remedy is an administrative act by a floor person, and there is no floor — §5 |

---

## 4. Decision D-M1-1 — fold-wins and mucked hole cards

This is the question M0 deferred here: the post-hand audit exposes hole cards
that TDA convention would never show, including on a hand that ends by folds
(`docs/AUDIT-M0-D1-D4.md:88-101`). D3 was closed as *not a defect at the named
site*, **not** as *no exposure exists*.

### 4.1 What actually happens today

**Protocol layer — every dealt seat's hole cards become public at hand end, on
every completed hand.**

* Settlement is gated on the audit. `_step_hand` opens the audit and refuses to
  settle until it completes for `PHASE_HAND_OVER` — the folds case — exactly as
  for `PHASE_SHOWDOWN` (`session.py:2346-2350`).
* The audit is a full-deck opening: every seat publishes a DLEQ-proven
  decryption share for **all 52 positions** (`mental_deal.py:766-788`,
  `deck_audit.py:55-69`). Both docstrings state the consequence in terms —
  "mucked and burned cards become public here" (`mental_deal.py:774`),
  "Gameplay consequence, accepted by design" (`deck_audit.py:33`).
* Once those shares are on the wire, every peer can combine every position.
  `MentalDealDriver.all_hole_cards()` is convenience over data the peer already
  holds (`mental_deal_driver.py:153-163`); gating it would be theatre, which is
  precisely why M0 changed no code there.

During play the protocol is *tight*: `_try_complete` refuses to combine another
seat's hole even when every share happens to be present
(`mental_deal.py:741-762`). The exposure is strictly post-hand.

**Client boundary — narrower, and inconsistent with itself.**

* A fold-win reveals nothing: the settled result carries no scored `runs`, so
  the reveal block never runs (`client_view.py:129-142`).
* A contested showdown reveals the hole cards of **every dealt seat in the
  map** — including seats that folded on an earlier street (same loop, which
  filters only on `is_you`).

### 4.2 The decision

**D-M1-1a — fold-wins: ADOPT the convention at the client boundary.**
A hand that ends by folds shows no hole cards to any player. This is today's
behaviour; the profile pins it so that a future refactor cannot lose it by
accident, and M7 owns a control that fires when it does.

**D-M1-1b — post-hand secrecy: OVERRIDE, stated in the product, not implied
away.**
Hole-card secrecy for a **completed** hand is not a property this protocol
provides and must never be advertised as one. A peer running modified software
learns every dealt seat's hole cards for the hand just completed, including on
a fold-win, because the audit that makes settlement trustworthy is the same
event that opens the deck. The trade is deliberate: the audit is the only point
in the protocol where a seat publishes a **proven** share for its **own** hole
cards, and settling without it has been examined and **REFUTED** by two
independent adversarial reviews (ROADMAP, M2 known limitations). We keep the
integrity guarantee and give up post-hand secrecy.

The honest consequence is that a UI-level muck would be a lie. This is why
§3.1 classifies the beaten-hand muck right as OVERRIDE rather than preserving
it cosmetically: showing "mucked" for a hand that any modified peer can read
would misrepresent the guarantee, which is worse than tabling.

**D-M1-1c — folded seats: never rendered.**
The client boundary must reveal, at a contested showdown, only the seats that
**reached** that showdown — the contested seats the engine records in
`result["shown"]` (`engine.py:982-1019`). A seat that folded on an earlier
street is never rendered to another player. There is no integrity argument for
rendering it: the audit needs those positions, the screen does not, and folding
ranges across a session are exactly the information TDA's convention protects.

D-M1-1c is **convention conformance, not a security control**, and must not be
described as one. It changes what the stock client shows; it cannot change what
a modified peer can compute, because D-M1-1b already concedes that. The two are
consistent: we conform to the convention where conforming is honest, and we
refuse to imply the convention is enforced.

Current behaviour deviates. It is recorded as gap **C1** in §7 and fixed in
**M7** — this document does not change engine or client behaviour.

### 4.3 What the product must say

Anything that describes the guarantee to a player — README, onboarding, the
client's own copy — must not claim that mucked or folded cards stay private
after the hand. The accurate statement is: *your hole cards are cryptographically
private while the hand is live; when the hand ends, the deck is opened so every
seat can prove nobody cheated.*

---

## 5. Collusion, soft play, and chip dumping are policy risks, not invariants

State this plainly, because the alternative is implying protection that cannot
be delivered: **in a table with no operator, this protocol cannot detect,
prevent, or remedy collusion, soft play, or chip dumping.**

Why each is out of reach:

* **They are sequences of legal actions.** Chip dumping is a legal all-in and a
  legal call. Soft play is a legal check. Nothing in the action stream
  distinguishes them from bad play, and the engine validates legality, not
  motive.
* **Side channels are outside the protocol.** Two players on a voice call share
  hole cards the protocol correctly refused to share. No amount of
  cryptographic work reaches that channel.
* **Identity is not distinctness.** A complete, legal seat→key map does not
  establish that two seats are two people. The roster is the host's assertion,
  and admission proves possession of the invite, which everyone invited holds
  (`docs/AUDIT-M0-D1-D4.md:57-67`; `session.py:1665-1672`). One human can hold
  two seats, and the protocol will authorize both correctly.
* **TDA's remedy does not exist here.** Penalties, forfeiture of chips, and
  disqualification are administrative acts by a floor person. There is no
  floor. There is also no eviction primitive to build one on: membership cannot
  shrink mid-hand (blocker **B1**).

What the protocol *does* guarantee, and which must not be confused with the
above: chips are conserved and the arithmetic is replicated
(`session.py:1881-1888`, `replica_table.py:251-272`), and **cryptographic**
misbehaviour is detected and attributed to a seat (`mental_deal.py:813-834`).
Cheating the *deck* is caught. Cheating the *game* between two consenting
players is not.

The only mitigation is out of band: play with people you are willing to sit
down with. That is product policy, and it does not become an invariant by being
written in a specification.

---

## 6. A cryptographic integrity failure is not a misdeal

TDA 35 and 36 exist because a physical procedural error has **no bad actor**.
That is exactly why "once substantial action occurs the hand must proceed" is a
sane remedy there — the error is noise, and unwinding it costs more than
absorbing it.

An invalid Bayer–Groth proof, a malformed ciphertext, a failed DLEQ, or a deck
that fails the multiset check is **evidence of attempted cheating**. "Play on"
is correct for the first category and unacceptable for the second. Rules 35 and
36 are therefore `N/A` (§3.5) and must never be reached for by analogy.

The distinction is structural in the code, not merely editorial:

| | Procedural (TDA 35/36) | Cryptographic integrity failure |
|---|---|---|
| Bad actor | none assumed | assumed, and named where the evidence names one |
| Remedy | correct and continue; substantial action bounds the correction | fail closed, no skip-and-continue (`mental_deal.py:37-41`) |
| Record | table talk | a `HandRecord` with an outcome and a `blamed_seat` (`session.py:2239-2281`) |
| Outcome vocabulary | "misdeal" | `VOID_PROTOCOL`, `VOID_EQUIVOCATION` (`session.py:1251-1254`, `session.py:2283-2293`) |

**The mechanical remedy currently looks similar, and that similarity must not be
mistaken for the classification.** A voided hand redeals the same seats at the
same button from the same pre-hand chain state (`session.py:2067-2076`) — which
is a live room's misdeal handling. What differs is that the void is classified,
recorded, announced, and attributed. What is missing is any *consequence* for a
seat proven to have cheated, because there is no eviction primitive (B1). A
peer that voids every hand is a denial-of-service the profile can classify but
the protocol cannot yet answer. Recorded in §9; it belongs to M2 and M7, not to
a rules document.

---

## 7. Conformance status, and the gaps M7 owns

Read against `holdem/engine.py`, `holdem/p2p/replica_table.py`,
`holdem/client_view.py` and `holdem/contract.py` at `c69a4bf`. **Nothing was
changed.**

### 7.1 Conformant today

| Behaviour | Evidence |
|---|---|
| Heads-up button, blinds, act order | `engine.py:471-473`, `613-617`, `830` |
| Odd chip walks forward from the button | `engine.py:969-977` |
| A short all-in does not reopen betting for prior actors | `engine.py:758-776` |
| Cards speak; settlement scores actual cards | `engine.py:910-915` |
| Showdown order | `engine.py:984-995` |
| Uncalled bet returned to a single top live total only | `engine.py:861-877` |
| Dead money never returns to a stack | `engine.py:861-863` |
| Side pots, merge, residual dead money | `engine.py:917-938` |
| Hidden information during play | `contract.py:46-85`, `mental_deal.py:741-762` |
| No burn cards | `deal_map.py:13` |
| Fold-win reveals nothing at the client | `client_view.py:129-142` |

### 7.2 Gaps — all owned by M7, none fixed here

| ID | Gap | Profile rule violated | Site |
|---|---|---|---|
| **B6** | cumulative short all-ins do not reopen betting; each all-in is judged alone against `min_raise` and never accumulated | §3.3, Rule 47 | `engine.py:756-776` |
| **C1** | at a contested showdown the client renders the hole cards of seats that **folded earlier**, not only the seats that reached showdown | §4.2 D-M1-1c | `client_view.py:129-142` |
| **C2** | a sub-minimum raise is silently coerced up to `min_to` instead of being rejected. Deterministic, so replicas converge — but a malformed action from a hostile peer still mutates state, and the profile says reject | §3.3, Rule 43 | `engine.py:747-750` |
| **C3** | `Engine.act(i, ...)` never checks `i == self.actor`. The replica layer gates it today, so the P2P path is safe; the engine itself is not | §3.3, Rule 53 | `engine.py:709`, gated at `replica_table.py:176-177` |
| **C4** | `contract.apply_command` sends the action string `"check"`, which `Engine.act` does not name. It matches no branch, so it silently discards the actor from `need_to_act` and advances the turn with no event and no `last_action`. Single-process path only — the P2P client sends `"call"` (`client_view.py:246-248`) | §3.3 "one canonical typed action, validated once" | `contract.py:106-107` vs `engine.py:713-747` |
| **C5** | straddle behaviour is unverified against TDA straddle provisions | §3.2 | `engine.py:585-600` |

C4 is new in this document; it was not in the research note. It is the same
class as C2 — an action outside the closed set is absorbed rather than refused
— and it is the reason the "one canonical typed action" invariant needs a test
rather than a sentence.

---

## 8. Consensus-critical fields

Two peers must not begin a hand while silently using different poker rules or
different timeout arithmetic. The full binding rides **M2**; this is the field
list the profile requires it to carry.

| Field | Bound | Note |
|---|---|---|
| `rules_profile` | **yes, M2** | `poker.tda.2024.nlhe.v1`, encoded per §1.2 |
| `T`, `delta`, `L` | **yes, M2** | timeout parameters; `T` is table policy, not a fixed rule (§3.4) |
| `G`, `E` | **no** | strictly derived (`G = L + Δ`, `E = T + G`). Transmitting them would let two peers disagree about values that have no independent existence |
| `timeout_policy_version` | **yes, M2** | so a future change is a clean wire break, not a silent split |
| deal policy | already bound | `session.py:895-896` |
| `seats_in`, button | already bound | frozen participant set and deal map |

The profile identifier and the timeout parameters are **separate** fields. They
are not folded into one string: the profile revision changes when a *ruling*
changes, and `T` changes when a *table* changes, and collapsing them would force
a rules-profile bump every time someone picked a different clock.

---

## 9. Known limitations

* **The identifier is specified and unbound.** Until M2 lands the binding, two
  peers can start a hand under different readings of this document and nothing
  refuses it. Scheduled, not overlooked (ROADMAP Decisions, 2026-08-18).
* **Post-hand hole-card secrecy does not exist** (§4.2b). Accepted, with the
  refutation of the alternative recorded in ROADMAP M2.
* **Collusion, soft play and chip dumping are unenforceable** (§5). No part of
  this document should be read as mitigating them.
* **A proven cheat still gets a redeal.** Void → redeal is the only remedy
  available, because membership cannot shrink mid-hand (B1). A peer that voids
  every hand is a denial of service this document can classify but not answer.
  M2 owns suspension; any exclusion policy is later still.
* **Several rule numbers are unverified in this repository** — every row marked
  **[U]** in §3. Their *substance* is what this profile adopts; the numbers must
  be confirmed against the published TDA 2024 text before being quoted as
  citations elsewhere.
* **The straddle is unverified** (C5), and it is a *rules* gap rather than a
  code gap: nobody has checked what the correct behaviour is.
* **`docs/TIMEOUT_SPEC.md` is known to be wrong in three places** and is
  rewritten in M3. §3.4 cites it for the currently implemented default only, not
  as a correct contract.

---

## 10. Change control

* This document is normative. Research belongs in `docs/research/`; promoting a
  candidate design into this file is a deliberate, reviewed act
  (`docs/COLLABORATION.md`).
* A ruling change requires a revision bump (§1.3) — not an in-place edit. The
  point of a pinned identifier is that "which rules were we playing?" has an
  answer after the fact.
* Every gap in §7 is closed by a change with a deliberate-break control that
  names the test it is the control for. A conformance fix with no control is not
  a conformance fix.
