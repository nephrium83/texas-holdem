# Professional Poker Rules Profile

**Profile identifier:** `poker.tda.2024.nlhe.v1`
**Type:** normative contract (see `docs/COLLABORATION.md`, "Research versus
contract"). Implementation that contradicts this document is a bug in the
implementation.
**Milestone:** M1. **Base:** `c69a4bf` (`main`).
**Normative source:** `docs/research/sources/poker-tda-2024-v1.0-longform.txt`
— the official Poker TDA 2024 Rules v1.0 (9 Oct 2024), rules, Recommended
Procedures and Illustration Addendum, with provenance and hashes in
`poker-tda-2024-v1.0-source.md`.
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
versions"). The pinned source is that edition and says so on its own first
line: *"2024 Poker TDA Rules, Version 1.0 (October 9, 2024)"* `src:1`. Adopting
a 2026 identifier before TDA publishes one would pin this protocol to a
document that does not exist.

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

## 2. Sources, and how a rule is cited here

| Source | Version | Role | Held where |
|---|---|---|---|
| **Poker TDA Rules** | **2024 v1.0, 9 Oct 2024** | primary normative base | in this repository — `docs/research/sources/poker-tda-2024-v1.0-longform.txt`, provenance and SHA-256 hashes in `poker-tda-2024-v1.0-source.md` |
| WSOP | 2026 Main Event | cross-check, clock policy | `docs/research/professional-poker-rules-audit.md` |
| GGPoker house rules | current | cross-check, online disconnect/timeout | `docs/research/professional-poker-rules-audit.md` |

The authoritative text — the 71 numbered rules, the 22 Recommended Procedures
and the Illustration Addendum — is a mechanical extraction from the official
DOCX whose SHA-256 the provenance note records. TDA rules used by permission of
the Poker TDA, Copyright 2026, <https://www.pokertda.com>, all rights reserved.

### 2.1 The citation rule

**No ruling in this document rests on a rule number this repository cannot
show.** Every citation resolves to a line of the pinned source, written
`src:NNN`, where `src` is
`docs/research/sources/poker-tda-2024-v1.0-longform.txt`. A reader checks a row
by opening that line. Three markers, and only three:

| Marker | Basis |
|---|---|
| **[V]** | **Verbatim** at the cited line of the pinned source. |
| **[D]** | **Derived** from cited [V] provisions. The derivation is written out in the row and *is* the citation. |
| **[H]** | **House.** The pinned source was **searched** and no provision governs this point. The row names what was searched. The ruling is ours and is not offered as a professional-rule citation. |

Two earlier markers are retired. `[R]` — "rule number and substance as recorded
in the research note" — is obsolete now that the text itself is present: every
row that carried it is re-cited [V] against the source, and two turned out to
need a different rule number than the note gave (§3.2 dead button is Rule 32,
not 34; §3.5 fouled decks is 35-E, not the whole of 35). An older draft's
"substance is TDA's, number unverified" was never a citation at all.

**[H] now carries a search rather than an absence of one.** In the previous
revision it meant "this repository has not established a provision", which
recorded *our* uncertainty rather than the ruleset's content — a reviewer
correctly refused it as a substitute for reading the source. With the text
pinned, an [H] row must name the provisions examined and say why none governs.
Exactly two rows survive that test: residual dead-money allocation (§3.1) and
timeout while cryptographically absent (§3.4). Attaching a contradicting
provision to an [H] row later is a ruling change and a revision bump (§1.3);
attaching a *confirming* one refines the citation and is not.

**How the negative claims were reached.** Where this document says a subject is
not addressed — the straddle, a second runout, the residual dead-money layer, a
participant whose duties outlive their hand — the basis is a **complete read of
the pinned source**, all 712 lines: 71 rules, 22 Recommended Procedures and the
Illustration Addendum, which is the entire published long-form document. That
is a bounded claim about one file whose hashes are recorded, and it is
falsifiable by opening it. It is **not** a claim about TDA rulings, forum
answers, seminar material or house rules that supplement the ruleset.

### 2.2 What each class has to prove

The citation burden is not the same for all four classes:

* **ADOPT** and **DIGITALIZE** change how a hand plays *because a professional
  rule says so*. The row must therefore show which rule: [V] at a line of the
  source, or an explicit [D] derivation from [V] rules, written out in the row.
  A ruling whose only support is our own reasoning is not an adoption of
  anything, however sensible the reasoning is.
* **OVERRIDE** means the ruling does **not** rest on a professional rule. Two
  branches, and the row says which:
  * **(a) departure** — a cited rule says one thing and this game does
    another. The row cites what is being departed from.
  * **(b) house** — the source was searched and no provision governs, so the
    ruling is ours. The row names the search.
* **N/A** asserts that a rule's failure mode **cannot occur here**. That claim
  is proved by our code, not by TDA's text. N/A rows cite the mechanism that
  makes the failure impossible, *and* the rule being retired, so §3.6 can show
  that no rule was quietly dropped.

**The checkable consequence: no [H] row is ADOPT or DIGITALIZE.** [H] is a
basis, not a class, and it cannot support a claim that a professional rule was
taken as written.

Three rows record that discipline working. Uncalled bets, side pots and
big-blind ante order were first written ADOPT on reasoning rather than text;
the previous revision demoted them to OVERRIDE (b) because no citation existed
here; this revision restores ADOPT because the pinned source supplies one
(R15-B and R65-A, R21 with the Rule 16 addendum, and RP-11 respectively).
**The ruling never changed through any of it** — only the claim about whose
rule it is. Labelling corrections are not ruling changes (§1.3), so the
identifier does not bump.

---

## 3. Classification matrix

**ADOPT** — the professional rule is taken as written; the digital form is the
same rule.
**DIGITALIZE** — the *consequence* is adopted, the *mechanism* is replaced,
because the mechanism assumes a human floor, a dealer, or physical objects.
**OVERRIDE** — the ruling does not rest on a professional rule. Either **(a)**
a cited rule says one thing and this game does another, and the row cites what
it departs from; or **(b)** the pinned source was searched and no provision
governs, so the ruling is **ours** (§2.2). Branch (b) rows are marked **[H]**
and are house rules, not adoptions.
**N/A** — the rule exists to manage a physical or procedural failure mode that
cannot occur here.

§3.1–§3.5 give the reasoned rows. **§3.6 maps all 71 rules and all 22
Recommended Procedures**, so "every relevant rule is classified" is a claim a
reader can check rather than one this document merely asserts.

### 3.0 Rule 1 is the rule we cannot have

> *"The best interest of the game and fairness are top priorities in
> decision-making. Unusual circumstances occasionally dictate that common-sense
> decisions in the interest of fairness take priority over technical rules.
> Floor decisions are final."* — TDA 2024 Rule 1 [V] `src:14`

**OVERRIDE (a).** Rule 1 is the ruleset's own admission that no written rule
set is complete, and its remedy is a person. This game has no floor, so it has
no Rule 1, and every downstream rule whose remedy is *"the floor will be
called"* (13-B, 15-A, 39-A, 51-B, 53-B, 57, 71, RP-14) loses its instrument
here. Where a rule mixes discretion with a flat statement, the flat statement
survives and only the discretion falls: 15-B and 65-A each hand a mucked or
killed hand to the floor **and** state without qualification that an uncalled
amount is returned, which is why §3.1 can adopt that clause from rules whose
procedure is otherwise unavailable. That is not a detail — it is why so much of
this profile is N/A rather
than DIGITALIZE. Where TDA resolves an ambiguity by discretion, we cannot
resolve it at all, so the ambiguity must be **designed out of existence**
instead: one canonical typed action, an exact amount, a validated actor, a
deterministic evaluator. A rule that survives only because a human can judge
the unusual case is a rule this protocol must not pretend to keep.

### 3.1 Cards, settlement, showdown

| Area | TDA 2024 | Class | Ruling | Evidence |
|---|---|---|---|---|
| Cards speak | 12 [V] `src:78` | ADOPT | *"Cards speak to determine the winner. Verbal declarations of hand value are not binding at showdown…"* The verified cards plus the evaluator determine the result; no UI text, chat, or declaration alters settlement | `engine.py:910-915` scores from `evaluate(p.hole + b)` |
| All-in hands tabled | 16 [V] `src:100`, addendum `src:552-572` | ADOPT | *"All hands will be tabled without delay once a player is all-in and all betting action by all other players in the hand is complete. No player who is either all-in or has called all betting action may muck their hand without tabling."* | `engine.py:1000-1002` `must_show` is true whenever `tabled` |
| Showdown order | 17-A [V] `src:104` | ADOPT | *"The last aggressive player on the final betting round (final street) must table first. If there was no final round bet, the player who would act first in a final betting round must table first (i.e. first seat left of the button in flop games…)"* | `engine.py:984-995` |
| Beaten hand may muck | 17-B [V] `src:106`; 13-B `src:84`; 14 `src:90` | **OVERRIDE (a)** | The right is verbatim: *"A non all-in showdown is uncontested if all but one player mucks face down without tabling. The last player with live cards wins and is not required to table the cards."* In the P2P game every contested seat is tabled and **there is no muck right** — the deck is opened at hand end regardless, so a muck would conceal nothing (§4.2b). The engine's muck path still exists and still runs in the single-process game, where the deck is not opened | `session.py:2354-2355` passes `force_tabled=True` for every P2P showdown; `engine.py:1000-1019`; §4 |
| Asking to see a hand | 18-B [V] `src:112` | DIGITALIZE | the **consequence** — *"If there was a river bet, any caller has an inalienable right to have the last aggressor's hand tabled on request ('the hand they paid to see')"* — is adopted and then some: every contested seat is tabled unconditionally, so the right is satisfied without a request, and without TDA's conditions on it. The **mechanism** (a request, and TD discretion over every other request) is replaced, because both the request channel and the discretion require a floor (§3.0). 18-A, which strips the right from a player who mucked, is moot for the same reason | `session.py:2354-2355`; §4.2 D-M1-1c |
| Playing the board | 19 [V] `src:116` | **N/A** | *"To play the board, players must table all hole cards to get part of the pot."* The rule polices a player's choice of what to reveal; no such choice exists — the audit reveals the cards and the evaluator uses the best five from seven whether or not the player would have shown them | `engine.py:910-915` |
| Odd chips | 20-A [V] `src:120` | ADOPT | *"the odd chip goes to the first seat left of the button"*, walking forward to the first winner. **Consensus-critical** — every replica must award the same chip | `engine.py:969-977` |
| Uncalled bet returned | 15-B [V] `src:96`; 65-A [V] `src:370` | ADOPT | both provisions state the return as settled background even in the two *adverse* cases — a hand mucked by mistake and a hand killed by the dealer: *"If cards are mucked and the player initiated a bet or raise not yet called, the uncalled amount will be returned"* (15-B), *"If the player initiated a bet or raise and hasn't been called, the uncalled amount will be returned"* (65-A). **[D]:** a rule that returns the uncalled amount even to a player whose hand is dead returns it a fortiori in the ordinary case, and TDA 2024 contains no provision anywhere that keeps an uncalled wager in the pot. The engine's condition — a *single* seat strictly above the second-highest live total — is that rule and an arithmetic identity: when two seats share the top total each was called by the other, so no amount is uncalled | `engine.py:861-877` |
| Dead money never returns | 30 [V] `src:174` | ADOPT | *"Their posted blinds and antes forfeit to the pot…"* Antes and dead blinds stay in the pot | `engine.py:861-863`, `settle` never refunds `total_dead` |
| Side pots | 21 [V] `src:124`; addendum Rule 16 Ex. 2–3 [V] `src:562`, `src:572` | ADOPT | *"Each side pot will be split separately"* (21), and the addendum fixes eligibility by matched contribution in worked numbers: the short all-in is *"the all-in who is only in for the main pot"*, while B's 1000 bet and C's call form *"the 2000 side pot between B and C"*, awarded before the main pot. That is exactly layering by committed total with each layer contested only by the seats that matched it. **[D]** for one implementation detail: two adjacent layers with *identical* eligible sets are merged, which is invisible to Rule 21 because splitting them separately and splitting the merged layer award the same chips to the same seats | `engine.py:917-932` |
| Residual dead money above every live stake | **[H]** | **OVERRIDE (b) — house** | a folded big blind's ante can sit above every *live* stake, funding no layer with an eligible contestant. It joins the top pot. **The search:** Rule 21 `src:124` splits side pots but does not build them; Rule 30 `src:174` forfeits blinds and antes *"to the pot"*, singular, and names no layer; Rule 20 `src:120` allocates odd chips, not dead stakes; 35-E `src:208`, 71-C `src:410` and RP-11 `src:492` create dead money without allocating it. No provision in the 2024 rules, Recommended Procedures or Addendum governs the residue. Ours, on the same arithmetic as the rows above: a stake no contesting seat matched cannot define its own eligible layer | `engine.py:933-938` |
| Run it twice | 38 [V] `src:220`; 39-A/B [V] `src:224-226` | ADOPT | *"The burn is always one card per street, never more"* (38), and Rule 39 reconstructs a **three-card flop and a single board** whenever dealing goes wrong. The ruleset deals one board per hand and provides no second runout anywhere — that is an affirmative single-board procedure, not an argument from silence. **No hand governed by this profile runs the board twice.** The engine's two-run path is a cash-game extension outside this tournament base, and is structurally impossible on the P2P path anyway: a second run deals from the deck, which mental poker cannot produce | `engine.py:884-898`, `engine.py:947`; `replica_table.py:29-31` pins `runs=1` |
| Rabbit hunting | 28 [V] `src:164` | **OVERRIDE (a)** | *"Rabbit hunting (revealing cards that would have come if the hand had not ended) is not allowed."* This game **cannot** keep that rule. The post-hand audit opens all 52 deck positions, so after any hand — including one that ends pre-flop by folds — every peer can read the board that would have come. It is structural, not a client behaviour: the same event that proves the deck was a deck reveals the undealt stub. No client should *display* a rabbit hunt, but no client can prevent one either (§4.2b) | `mental_deal.py:766-788`, `deck_audit.py:55-69` |

### 3.2 Position, blinds, deal

| Area | TDA 2024 | Class | Ruling | Evidence |
|---|---|---|---|---|
| Dead button | 32 [V] `src:184`; 34-A [V] `src:192` | ADOPT | *"Tournament play will use a dead button."* Forward-moving BB anchor; SB and button trail it and may land on vacated seats, and a seat that owes a small blind posts it dead. The research note cited Rule 34 for this; 34 is button *placement and movement error*, and the dead button itself is Rule 32 | `engine.py:462-506`, `engine.py:505-506`, `engine.py:578` |
| Heads-up button | 34-B [V] `src:194` | ADOPT | *"Heads-up, the small blind is the button, is dealt the last card, and acts first pre-flop and last on all other betting rounds."* All four clauses, including dealt-last | `engine.py:471-473`, `engine.py:609-617`, `engine.py:830` |
| Big-blind ante order | RP-11 [V] `src:492` | ADOPT | *"If a single-payer ante is used, the big blind ante format (BBA) with big-blind-first calculation is recommended."* **Big-blind-first is the whole ruling**: when the big blind cannot cover both posts, its chips fill the **blind** first and only the remainder becomes the ante. The engine does exactly that — `_post` takes `min(amount, stack)` for the blind, then `_post_dead` takes what is left for the ante — so `engine.py:569`'s "TDA order" comment is corroborated by the source, not merely asserted. The order is load-bearing: it decides whether a short big blind's last chips land in the **live** bet or in **dead** money, which changes pot eligibility and the amount others face. **Two things this row does not claim.** RP-11 is a *Recommended* Procedure, advisory to houses; this profile makes it **binding** here, and that mandatory force is ours, not TDA's. And RP-11 recommends a calculation order without spelling out the short-stack arithmetic, so the reading above is stated explicitly for a reviewer to check against `src:492`. **Consensus-critical** | `engine.py:569-571`, `_post` (`621-629`) then `_post_dead` (`631-642`) |
| Burn cards | 38 [V] `src:220` | **N/A** | *"The burn is always one card per street, never more"* — a rule about protecting a physical stub. No burn cards exist here: the deal map is `2m + 5` positions and nothing else | `deal_map.py:13`, `engine.py:814-825` |
| Straddle | 51-B [V] `src:298`; 43 addendum Ex. 2 [V] `src:596` | ADOPT | **No hand governed by this profile may contain a straddle.** The blind structure is stated, not inferred: *"In blind games the posted BB is the pre-flop opener"* (51-B), and the Rule 43 addendum works a pre-flop example — an under-the-gun all-in for 150 over a 100 blind — in which *"The 100 is still the 'largest bet or raise of the current round'"* `src:596`: the big blind, and nothing else, opens the betting and sets the first minimum raise. A straddle is a third voluntary blind that displaces the BB as opener and doubles that minimum, which 51-B does not admit; and the word *straddle* appears nowhere in the 2024 rules, Recommended Procedures or Addendum. Adopting the pinned structure therefore excludes it. `ReplicaTable.start_hand` calls the engine without `straddle_fn`, so the branch is already unreachable on the P2P path — but by omission, not refusal, which is gap **C5**. Where a single-process caller supplies `straddle_fn` it is a cash-game option outside this tournament base, in the form the engine implements: 3+ handed, big-bet only, UTG, a live post of 2×BB. It must never be enabled on the P2P path unless it first becomes a bound table parameter (§8) — two replicas disagreeing about it differ in blinds, in `min_raise` and in the first actor before a single action is taken | `engine.py:585-600`; `replica_table.py:111` passes no `straddle_fn`; gap **C5** |

### 3.3 Betting

| Area | TDA 2024 | Class | Ruling | Evidence |
|---|---|---|---|---|
| No maximum bet (NLHE) | 48 [V] `src:282`; 42 [V] `src:248`; 54-D [V] `src:322` | ADOPT | a bet is bounded only by stack. **The derivation:** the ruleset is written *for* a betting structure it does not itself define — it says *"there is no cap on the number of raises in no-limit and pot-limit"* (48), prescribes how a raise is made *"in no-limit or pot-limit"* (42), and rules that *"Declaring 'I bet the pot' is not a valid bet in no-limit"* (54-D), which only makes sense where the pot does not bound the bet. The `nlhe` field of the identifier (§1.1) is where this profile picks the structure those rules assume | `engine.py:664-705`; `replica_table.py:72`, `structure="No-Limit"` |
| Minimum raise amount | 43-A [V] `src:252`; addendum `src:590-602` | ADOPT | *"A raise must be at least equal to the largest prior full bet or raise of the current betting round"* — and the addendum insists on the reading that matters: the minimum is the largest **increment** added, *"not 3600 (C's total bet), but only 2000, the additional raise action that C added"* `src:594`. The engine stores exactly the increment | `engine.py:756-765`, `min_raise = raise_size` where `raise_size = p.bet - current_bet` |
| The 50% rounding remedy | 43-A [V] `src:252`; 45 [V] `src:262-264` | DIGITALIZE | *"A player who raises 50% or more of the largest prior bet but less than a minimum raise must make a full minimum raise. If less than 50% it is a call unless 'raise' is first declared or the player is all-in."* The **consequence** — a sub-minimum raise never stands as a raise — is adopted; the **50% threshold itself is discarded**, because it is a remedy for an ambiguous physical gesture (which chips were pushed, in how many motions), and no gesture exists. A sub-minimum raise is **rejected**, never rounded up and never silently reinterpreted as a call | current code coerces upward — gap **C2**, §7 |
| Correcting an underraise | 52-A [V] `src:304` | DIGITALIZE | *"opening or raising less than the minimum legal amount is corrected anywhere on the current street"* — a repair for chips that are **already in the pot** and cannot be un-pushed. A typed action is validated before it becomes state, so the invalid bet never needs correcting; the same consequence (no under-minimum bet stands) is reached by refusal instead of repair. Note this is where our departure is sharpest: TDA corrects *upward*, which is what the engine does today, and the profile still says reject — because a correction rule is safe only when the actor is a human at a table and not a peer sending arbitrary bytes | gap **C2**, §7 |
| Re-opening the bet | 47-A [V] `src:276`; addendum Ex. 1 [V] `src:650-658` | ADOPT | *"an all-in wager (or cumulative multiple short all-ins) totaling less than a full bet or raise will not reopen betting for players who have already acted and are not facing at least a full bet or raise when the action returns to them"* — so cumulative short all-ins that *do* total a full raise **must** reopen it; the addendum works the case (25 + 75 = a full 100 raise, betting reopens). The second clause is adopted too: *"the minimum raise is always the last full valid bet or raise of the round"* | reopening: current code judges each all-in alone — gap **B6**, §7. Min-raise clause: conformant, `engine.py:764-765` updates `min_raise` only when the raise is full |
| Committed chips stay committed | 50-A [V] `src:290` | ADOPT | *"Action in turn is binding and commits chips to the pot that stay in the pot."* This is the professional statement of standing invariant 4 | `engine.py:644-650` `_commit`; ROADMAP standing invariant 4 |
| Action out of turn | 53-A/B [V] `src:310-312` | DIGITALIZE | the **consequence** — an out-of-turn action must not stand as if it were in turn — is adopted; the **machinery** is dropped entirely. TDA backs the action up, keeps it binding if the action to the player did not change, and hands the skipped player's fate to the floor, all because the chips were physically pushed and the information is already public. Here the action is rejected before it becomes game state: no backup, no binding, no penalty, no floor (§3.0) | `replica_table.py:176-177` rejects a non-actor; engine-level guard is gap **C3** |
| Binding declarations, undercalls | 51 [V] `src:296-300` | **N/A** | the rule governs the gap between what a player *said* and what they *pushed*. One canonical typed action carries both; it is validated once and accepted once, so an undercall cannot be expressed | `client_view.py:244-251` |
| Accepted action | 49 [V] `src:286` | **N/A** | *"It is the caller's responsibility to determine the correct amount of an opponent's bet before calling, regardless of what is stated by others."* The rule allocates the cost of a miscount; the amount to call is replicated state, computed identically on every replica, and no dealer or player announces it | `engine.py:664-668` `legal()` derives `to_call` from state |
| Oversized chips, multi-chip bets, prior-bet chips, over-betting for change | 44 [V] `src:258`; 45 [V] `src:262-264`; 46 [V] `src:268-272`; 61 [V] `src:352` | **N/A** | four rules, one physical cause: chips of fixed denomination pushed across a table. No chips are pushed |
| String bets, verbal-vs-chip conflict, gestures, invalid and conditional declarations, non-standard folds | 3 [V] `src:22`; 40 [V] `src:236-240`; 55 [V] `src:326`; 56 [V] `src:330`; 57 [V] `src:334`; 58 [V] `src:338`; 59 [V] `src:342-344` | **N/A** | the whole class is eliminated by typed actions: no gesture to interpret, no moment between two chips leaving a hand, no unofficial term to construe, no way to declare a future action. Each of these rules resolves an ambiguity at TD's discretion, so each would have needed a floor we do not have (§3.0) | `client_view.py:244-251` |
| Count of an opponent's stack | 60 [V] `src:348` | **N/A** | every stack is replicated state, exact and continuously visible; there is nothing to request and nothing to estimate | `replica_table.py:251-272` |

### 3.4 Clock and absence

| Area | TDA 2024 | Class | Ruling | Evidence |
|---|---|---|---|---|
| Calling for a clock | 29 [V] `src:168` | DIGITALIZE | *"A player on the clock has up to 25 seconds plus a 5 second countdown to act. If the player faces a bet and time expires, the hand is dead; if not facing a bet, the hand is checked."* The **consequence** is adopted verbatim, including the fold/check split; the 25 + 5 becomes a single deterministic `T = 30 s`; the **authority** to declare expiry is replaced, because there is no floor to call (§3.0) — and the same sentence's *"TDs may adjust the time allowed"* is why `T` is a table parameter | `TIMEOUT_SPEC.md:69-79` (betting default 30 s); the corrected contract is **M3** |
| Absent player, blinds forfeit | 30 [V] `src:174` | ADOPT | *"Players not then at their seats may not look at their cards which are killed immediately. Their posted blinds and antes forfeit to the pot…"* This is the professional precedent for the non-profitability invariant: absence forfeits the hand and the chips already committed, and does not refund them | ROADMAP standing invariants 1 and 4 |
| Remaining at the table with action pending | 31 [V] `src:178` | ADOPT, and it stops short | *"Players with live hands (including players all-in or otherwise finished betting) must remain at the table for all betting rounds and showdown."* Adopted for the seats it covers — a seat that is all-in or has finished betting still owes the table its presence. Note precisely where it stops: the duty attaches to a **live hand**, so a folded seat has none, which is exactly the seat the next row is about | ROADMAP standing invariant 2 |
| Timeout while **cryptographically** absent | **[H]** | **OVERRIDE (b) — house** | a seat that has folded, and therefore has no live hand, is still a **required cryptographic participant** until the hand is cryptographically complete. **The search:** Rule 30 `src:174` covers absence at the deal; Rule 31 `src:178` covers presence during the hand but binds only *live* hands; Rule 29 `src:168` covers slowness while acting; 71-A/C `src:406`, `src:410` blind out an absent player and kill the hands of a player on penalty — in every case the absent player's obligation ends when their hand ends, because at a physical table nothing else is owed. Nothing in the 2024 rules, Recommended Procedures or Addendum contemplates a participant whose duties **outlive their hand**, for the good reason that no such role exists at a table where the dealer holds the deck. Ours, and it is suspension semantics — **M2/M3** — not a poker rule | ROADMAP standing invariant 2, blocker B1 |

**Duration is event policy, not a rule.** WSOP 2026 ran a 20 s clock with six
30 s extension chips on Day 7 of the Main Event and removed it for the final
table (research note, "Source hierarchy"). That is a cross-check, not a rule to
classify, and it is why the Rule 29 row above adopts the **consequence** while
leaving the number to the table: `T` is a table parameter, and a
**consensus-critical** one (§8).

### 3.5 Errors, integrity, ethics

| Area | TDA 2024 | Class | Ruling | Evidence |
|---|---|---|---|---|
| Misdeals | 35-A–D [V] `src:200-206` | **N/A** | boxed cards, cards dealt to the wrong seat, the wrong number of cards — every listed cause is a hand mishandling physical objects. There is no physical dealing. Explicitly **not** the home for cryptographic failures — §6 |
| Fouled deck | 35-E [V] `src:208` | DIGITALIZE | *"If 2 or more cards of the same suit and rank are found, the deck is fouled… If a fouled deck is discovered, regardless of SA, play will stop and all bets will be returned."* **The one professional provision that does treat deck integrity as a stop-and-unwind event**, and it is the analogue our void-and-redeal implements: the **consequence** — halt regardless of how far the hand went, restore the chips — is adopted; the **mechanism** is replaced, because the detector is a multiset and proof check rather than an eye, and the remedy restores the pre-hand chain state rather than pushing chips back across felt. Note that TDA itself, in 35-D, carves the fouled deck out of "play on": *"a misdeal cannot be declared; the hand must proceed **unless the deck is fouled**"* — so the distinction §6 draws is the ruleset's own, not an invention of ours | `session.py:2067-2076` redeals the same seats at the same button from the pre-hand chain state; §6 |
| Substantial action | 36 [V] `src:212` | **N/A** | exists to bound the correction of physical errors; "substantial action occurred, play on" must never be applied to an integrity failure — §6, and 35-E above |
| Exposed cards, dropped cards, accidentally killed hands | 65 [V] `src:370-372`; 68 [V] `src:394` | **N/A** | eliminated by a cryptographic deck: no card has a physical face to expose and no seat holds one. (The *post-hand* exposure this protocol does have is a different matter entirely, and is ruled on in §4) | `mental_deal.py:741-762` |
| Disputed hands and pots | 22 [V] `src:128` | **N/A** | *"The reading of a tabled hand may be disputed until the next hand begins… Accounting errors… may be disputed until substantial action occurs on the next hand."* Both windows exist because a human read the hand and a human pushed the chips. Settlement here is a deterministic function of replicated state, so a reading dispute has no object; an accounting disagreement between peers is a **desync**, detected by digest comparison rather than argued and ruled on | `replica_table.py:42-45`, `state_digest()` |
| Floor decisions | 1 [V] `src:14` | **OVERRIDE (a)** | there is no floor, and no rule that ends in one survives — §3.0 |
| Player identity | 4 [V] `src:26` | **OVERRIDE (a) — policy, not invariant** | *"Players must be clearly identifiable at all times."* A key is not a person. Admission proves possession of an invite, and one human may hold two seats with both correctly authorized — §5 |
| Electronic devices and strategy tools | 5 [V] `src:30-36` | **OVERRIDE (a) — policy, not invariant** | *"Betting apps, charts, and other poker strategy tools may not be used at the table."* Unenforceable by construction: every seat **is** a program on a general-purpose computer, and nothing distinguishes a player's action from a solver's. Stating it as a rule we keep would be the same overclaim §5 refuses |
| One player to a hand, no disclosure | 67 [V] `src:382-390` | **OVERRIDE (a) — policy, not invariant** | *"players, whether in the hand or not, must not:"* — first item, *"Discuss contents of live or mucked hands"* — and *"One-player-to-a-hand is in effect."* The prohibited channel is entirely outside the protocol — §5 |
| Ethical play — soft play, chip dumping, collusion | 69 [V] `src:398`; 71 [V] `src:406-412` | **OVERRIDE (a) — policy, not invariant** | *"Poker is an individual game. Soft play will result in penalties, which may include chip forfeiture and/or disqualification. Chip dumping and other forms of collusion will result in disqualification."* The prohibition is unambiguous and this protocol **cannot** enforce it. Every remedy Rule 71 defines — warning, missed-hand penalty, forfeiture, disqualification, *"chips of a disqualified player shall be removed from play"* — is an administrative act by a director. Our own identifier records that authority in its `tda` field (§1.1). A table with no director cannot inherit a remedy whose instrument is a director, and there is no eviction primitive to build one on (B1) — §5 |
| Etiquette violations and penalties | 70 [V] `src:402`; 71 [V] `src:406-412` | **N/A** | the enforcement machinery has no holder here; listed so §3.6 does not appear to drop it silently. Where an etiquette violation has a *mechanical* form it is handled mechanically instead — persistent delay by the clock (§3.4), repeated out-of-turn action by rejection (§3.3) |

### 3.6 Complete coverage map

The rows above argue the rules that needed argument. This map exists so that
"every relevant professional rule is classified" is **checkable**: all 71 rules
and all 22 Recommended Procedures appear exactly once, whether or not they
needed a paragraph. The **Where** column points at the section that rules on
the rule; a blank one means this map is its only home and nothing further is
owed on it.

One class appears only here. **OUT OF SCOPE** means the rule governs
multi-table tournament *administration* — registration, seat draws, table
balancing, chip races, level clocks, staff procedure. This game is a single
self-selected table with a fixed roster, no operator, no chips of denomination
and no level structure, so these rules have no subject matter here. That is a
statement about the rule's subject, not about our conformance, and each is
listed individually rather than waved at as a group.

| Rules | Subject | Class | Where |
|---|---|---|---|
| 1 | floor decisions | OVERRIDE (a) | §3.0 |
| 2 | player responsibilities | N/A — the duties are protecting cards, following action, acting in turn, tabling properly; each is a protocol guarantee here or is covered below | §3.3 |
| 3 | official terminology and gestures | N/A | §3.3 |
| 4 | player identity | OVERRIDE (a) — policy | §3.5, §5 |
| 5 | electronic devices, strategy tools | OVERRIDE (a) — policy | §3.5 |
| 6 | official language | OUT OF SCOPE — house posting |
| 7–11 | random seating, alternates and late registration, special needs, broken tables, table balancing | OUT OF SCOPE — no seat pool, no second table |
| 12 | cards speak | ADOPT | §3.1 |
| 13 | tabling cards, killing a winning hand | N/A — no physical tabling; every contested seat is revealed by the audit | §3.1, §4 |
| 14 | live cards at showdown | N/A — no retrievable-versus-irretrievable distinction exists | §3.1 |
| 15-A | one card tabled, floor called | N/A — no floor, no partial tabling | §3.0 |
| 15-B | uncalled amount returned | ADOPT for that clause; the surrounding procedure (hold the cards, call the floor, no refund of called bets) is N/A | §3.1, §3.0 |
| 16 | face up for all-ins | ADOPT | §3.1 |
| 17-A | showdown order | ADOPT | §3.1 |
| 17-B | uncontested showdown, muck right | OVERRIDE (a) | §3.1 |
| 18 | asking to see a hand | DIGITALIZE | §3.1 |
| 19 | playing the board | N/A | §3.1 |
| 20 | odd chips | ADOPT | §3.1 |
| 21 | side pots | ADOPT | §3.1 |
| 22 | disputed hands and pots | N/A | §3.5 |
| 23 | new hand, new limits | OUT OF SCOPE — no level clock |
| 24 | chip race, scheduled color-ups | OUT OF SCOPE — stacks are integers, no denominations |
| 25 | chips visible and countable | N/A — replicated state is exact and always visible | §3.3 |
| 26 | deck changes | N/A — the rule exists because a physical deck wears and can be marked; every hand here is dealt from a freshly generated verifiable deck |
| 27 | re-buys | OUT OF SCOPE — no tournament registration model |
| 28 | rabbit hunting | OVERRIDE (a) | §3.1, §4.2 |
| 29 | calling for a clock | DIGITALIZE | §3.4 |
| 30 | at your seat, live hands, blinds forfeit | ADOPT | §3.4 |
| 31 | at the table with action pending | ADOPT, stopping at live hands | §3.4 |
| 32 | dead button | ADOPT | §3.2 |
| 33 | dodging blinds | N/A — the remedy is a penalty and there is no authority to impose one; the dead button already removes the advantage | §3.2, §3.5 |
| 34-A | button placement and movement errors | ADOPT — and unreachable: button movement is derived identically on every replica | §3.2 |
| 34-B | heads-up button | ADOPT | §3.2 |
| 35-A–D | misdeals | N/A | §3.5, §6 |
| 35-E | fouled deck | DIGITALIZE | §3.5, §6 |
| 36 | substantial action | N/A | §3.5, §6 |
| 37 | button with too few cards | N/A — the deal map fixes the card count structurally | §3.2 |
| 38 | burns after substantial action | N/A (burns); ADOPT for one board per hand | §3.1, §3.2 |
| 39 | irregular flops, premature cards, reshuffling | N/A — the board is a fixed set of deal-map positions revealed in order | §3.2 |
| 40–41 | methods of betting and calling | N/A | §3.3 |
| 42 | methods of raising | ADOPT via the no-limit structure; the one-motion mechanics are N/A | §3.3 |
| 43-A | raise amounts and the 50% remedy | ADOPT (minimum) + DIGITALIZE (remedy) | §3.3 |
| 43-B | "raise" plus an amount is the total | N/A — the typed action carries a total, unambiguously | §3.3 |
| 44–46 | overchips, multi-chip bets, prior-bet chips | N/A | §3.3 |
| 47-A | re-opening the bet, cumulative short all-ins | ADOPT | §3.3, gap **B6** |
| 47-B | limit re-opening | N/A — not a limit game | §3.3 |
| 48 | number of allowable raises | ADOPT — no cap | §3.3 |
| 49 | accepted action | N/A | §3.3 |
| 50 | acting in turn; committed chips stay committed | ADOPT | §3.3 |
| 51 | binding declarations, undercalls | N/A | §3.3 |
| 52 | incorrect bets, underbets, underraises | DIGITALIZE | §3.3 |
| 53 | action out of turn | DIGITALIZE | §3.3, gap **C3** |
| 54 | pot size, pot-limit bets | N/A — not a pot-limit game; 54-D supports the no-limit reading | §3.3 |
| 55–59 | invalid declarations, string bets, non-standard betting and folds, conditional declarations | N/A | §3.3 |
| 60 | count of an opponent's stack | N/A | §3.3 |
| 61 | over-betting expecting change | N/A | §3.3 |
| 62 | all-in with chips found behind | N/A — a stack cannot be hidden from replicated state | §3.3 |
| 63–64 | chips out of view, lost and found chips | N/A — chips are integers in state, not objects |
| 65 | accidentally killed, fouled or exposed hands | N/A | §3.5 |
| 66 | dead hands and mucking in stud | N/A — not a stud game |
| 67 | one player to a hand, no disclosure | OVERRIDE (a) — policy | §3.5, §5 |
| 68 | exposing cards, proper folding | N/A | §3.5 |
| 69 | ethical play, soft play, chip dumping | OVERRIDE (a) — policy | §3.5, §5 |
| 70 | etiquette violations | N/A | §3.5 |
| 71 | warnings, penalties, disqualification | N/A — no authority, and no eviction primitive (B1) | §3.5, §5 |
| RP-1–RP-3 | all-in buttons, bringing in bets, personal belongings | N/A — table furniture and dealer practice |
| RP-4–RP-5 | disordered stub, prematurely dealt cards | N/A — no stub to disorder; see Rule 39 |
| RP-6–RP-9 | player movement, dealer pushes, hand-for-hand, final-table size | OUT OF SCOPE — multi-table administration |
| RP-10 | stud dealing procedures | N/A — not a stud game |
| RP-11 | big blind ante format, big-blind-first calculation | ADOPT (made binding here) | §3.2 |
| RP-12–RP-13 | dealers announcing bets, stacking split pots | N/A — no dealer |
| RP-14 | randomness for uncovered situations | **OVERRIDE (a)** — the escape hatch of last resort, and the same one Rule 1 provides: it authorises a TD to *design* a remedy. There is no TD, and a remedy invented at runtime by one peer is a consensus split. Uncovered situations must be settled in this document before they occur, which is what this profile is for |
| RP-15–RP-16 | staff communication, absent player on a breaking table | OUT OF SCOPE — staff procedure |
| RP-17–RP-18 | draw betting, order of mixed games | N/A — not draw, not a mixed game |
| RP-19 | reducing stalling | DIGITALIZE — the clock (§3.4) is our only instrument; the rest are house practices |
| RP-20–RP-22 | deck preparation, spreading the pot, non-denominational items | N/A — no physical deck, pot or bounty chips |

**Nothing in the 2024 ruleset is unclassified.** Two rows carry the [H] house
basis (§3.1 residual dead money, §3.4 cryptographic absence), and both name the
provisions searched. Every other row rests on a cited line of the source or on
a stated N/A mechanism.

---

## 4. Decision D-M1-1 — fold-wins and mucked hole cards

This is the question M0 deferred here: the post-hand audit exposes hole cards
that the professional rules would never show — Rule 17-B [V] `src:106` lets a
beaten hand muck unseen, and Rule 28 [V] `src:164` forbids revealing the cards
that would have come — including on a hand that ends by folds
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

**The audit opens all 52 positions, not just the ones that were dealt**, so the
same concession covers the **undealt** cards: the board that would have come on
a hand that ended early is readable afterwards. That is a departure from Rule
28 [V] `src:164`, which forbids rabbit hunting outright, and §3.1 classifies it
as one. No client should render it — showing a would-have-been board is a
product choice we decline — but no client can prevent another peer computing
it, and the profile refuses to describe a display convention as a protection.

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
ranges across a session are exactly the information the professional rules
protect — Rule 17-B [V] `src:106` lets a hand end without ever being shown, and
Rule 67 [V] `src:382-390` forbids even *discussing* the contents of a mucked
hand.

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
after the hand, and must not claim the audit proves nobody cheated. The
accurate statement is:

> *Your hole cards are cryptographically private while the hand is live. When
> the hand ends the whole deck is opened, so every seat can check the deck
> itself: that what was dealt really was one complete 52-card deck with no card
> substituted, duplicated or dropped, and that every seat's contribution to
> unscrambling it is proven against the key that seat published. Opening the
> deck is also what makes every dealt seat's hole cards — and the cards that
> were never dealt — readable once the hand is over.*

Two things that copy deliberately does not say, because they are not true:

* **It does not say nobody cheated.** The audit establishes the deck and share
  properties named above and detects the failures listed in
  `deck_audit.py:8-18`. It says nothing about collusion, soft play, or chip
  dumping, which are sequences of legal actions and are not detectable at all
  (§5).
* **It does not say a failed audit convicts anyone.** A failure means the state
  could not be verified; it does not establish a cause, and it names a seat
  only where the evidence identifies one (§6).

An earlier revision of this document prescribed "so every seat can prove nobody
cheated." That is the overclaim §5 and §6 exist to refuse, and no product copy
should carry it.

Checked at `c69a4bf`: no shipped copy carries it either. The README describes
the deal as verifiable and fail-closed without claiming post-hand secrecy or
the absence of cheating (`README.md:106-138`), and the onboarding flow makes no
cryptographic claim at all. This section is therefore a constraint on copy
written from now on, not a repair of copy already written — which is why it
produces no gap in §7.2.

---

## 5. Collusion, soft play, and chip dumping are policy risks, not invariants

State this plainly, because the alternative is implying protection that cannot
be delivered: **in a table with no operator, this protocol cannot detect,
prevent, or remedy collusion, soft play, or chip dumping.**

The professional ruleset is not vague about any of it. Rule 69 [V] `src:398`:
*"Poker is an individual game. Soft play will result in penalties, which may
include chip forfeiture and/or disqualification. Chip dumping and other forms
of collusion will result in disqualification."* Rule 67 [V] `src:382-390` adds
one-player-to-a-hand and forbids discussing the contents of live or mucked
hands. We adopt neither, because adopting a rule means being able to say what
happens when it is broken, and here nothing happens.

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
* **TDA's remedy does not exist here.** Rule 71 [V] `src:406-412` is the whole
  enforcement estate — warnings, missed-hand and missed-round penalties,
  forfeiture, disqualification, and *"chips of a disqualified player shall be
  removed from play"* — and every item on it is an administrative act by a
  floor person. There is no floor (§3.0). There is also no eviction primitive
  to build one on: membership cannot shrink mid-hand (blocker **B1**).

What the protocol *does* guarantee, and which must not be confused with the
above: chips are conserved and the arithmetic is replicated
(`session.py:1881-1888`, `replica_table.py:251-272`), and cryptographic
evidence that fails to verify is **detected**, and attributed to the seat that
authored it wherever the evidence identifies one (`mental_deal.py:813-834`;
§6). Cheating the *deck* is caught — though catching it is not always the same
as naming who did it, and naming a seat is not the same as proving intent (§6).
Cheating the *game* between two consenting players is not caught at all.

The only mitigation is out of band: play with people you are willing to sit
down with. That is product policy, and it does not become an invariant by being
written in a specification.

---

## 6. A cryptographic integrity failure is not a misdeal

TDA 35-A–D and 36 exist for errors where **the facts are still known**. A
misdealt card, a card flashed by a dealer's hand: everyone saw what happened,
the correction is obvious, and *"once substantial action occurs… the hand must
proceed"* (35-D [V] `src:206`) is a sane remedy precisely because absorbing the
error costs less than unwinding it.

An invalid Bayer–Groth proof, a malformed ciphertext, a failed DLEQ, or a deck
that fails the multiset check is a different kind of event: **the protocol
cannot establish that the state is what it must be.** The facts are not known.
That is why "play on" is available for the first category and unavailable for
the second — continuing would settle real chips against state nobody can
verify. Rules 35-A–D and 36 are therefore `N/A` (§3.5) and must never be
reached for by analogy.

**The pinned source draws the same line, and this is worth stating precisely,
because an earlier revision of this document did not know it.** Rule 35-D
carves one exception out of play-on in its own sentence: *"a misdeal cannot be
declared; the hand must proceed **unless the deck is fouled**."* And 35-E [V]
`src:208` supplies the matching remedy: *"If 2 or more cards of the same suit
and rank are found, the deck is fouled… If a fouled deck is discovered,
regardless of SA, play will stop and all bets will be returned."* A duplicate
card is precisely the physical form of "the deck was not a deck", which is what
our multiset check tests, and TDA's answer to it is to stop the hand however
far it has gone and unwind the chips.

So the distinction this section draws is **not** ours against the professional
ruleset; it is the professional ruleset's own, and our void-and-redeal is the
digital form of 35-E rather than an override of 35 (§3.5 classifies it
DIGITALIZE). What remains genuinely ours is everything 35-E has no vocabulary
for: a *proof* that fails rather than a card that is seen, a failure that may
be a defect or a skew rather than a mishandled deck, and an author to attribute
it to. Those are the subject of the rest of this section.

**This is a statement about verification, not about intent.** An integrity
failure does not prove attempted cheating. The same detector fires for a
software defect, a version skew between two builds, truncated or corrupted
data, and an ordinary lifecycle fault: this repository already records honest
re-sends that are indistinguishable from equivocation (**B9**), and a
reconnecting peer that derives a different `x_share` and aborts the hand
*blaming the honest returning seat* (**B7**). We fail closed **because** the
failure does not tell us which of those it is — not because we have concluded
it is an attack. A profile that claimed otherwise would assert a finding the
evidence does not support and would turn every honest fault into an accusation.

This **supersedes the research note's wording**, which called such a failure
"evidence of attempted cheating"
(`docs/research/professional-poker-rules-audit.md`, "Cryptographic integrity
failure is not a misdeal"). The classification that note argued for stands and
is adopted in full; the inference about intent does not, and the note remains
in `docs/research/` as research rather than being edited to match.

Attribution follows the same discipline, and the implementation already states
it: `MentalDeal` is *"fail-closed, with attribution wherever the evidence
identifies a seat"* (`mental_deal.py:37-51`). A bad proof-of-possession, an
out-of-turn shuffle, a bad decryption proof and an invalid prevention proof
each pin their author; the detection-only multiset check sees only the final
deck, says only that the chain was corrupted somewhere, and names **no** seat
(`mental_deal.py:826-834`, `blame = None`). So a `blamed_seat` is a fact about
the transcript — *this seat authored evidence that did not verify* — and not a
verdict about a person. Read alongside B7 and B9, the seat named may be the
honest one. No remedy here is entitled to treat attribution as proof of
cheating, and none does.

What survives all of that is the classification, and it is structural in the
code rather than merely editorial:

| | Procedural (TDA 35-A–D / 36) | Cryptographic integrity failure |
|---|---|---|
| What the failure establishes | the facts are known; only the procedure went wrong | the state cannot be verified; the failure alone does not determine the cause (defect, skew, corruption, or attack) |
| Remedy | correct and continue; substantial action bounds the correction | fail closed, no skip-and-continue (`mental_deal.py:37-41`) |
| Attribution | not at issue; no author is in question | the seat that authored the unverifiable evidence, **where the evidence identifies one**; otherwise none (`mental_deal.py:826-834`) |
| Record | table talk | a `HandRecord` with an outcome and a `blamed_seat` (`session.py:2239-2281`) |
| Outcome vocabulary | "misdeal" | `VOID_PROTOCOL`, `VOID_EQUIVOCATION` (`session.py:1251-1254`, `session.py:2283-2293`) |

**The mechanical remedy resembles a live room's, and now we know which one.** A
voided hand redeals the same seats at the same button from the same pre-hand
chain state (`session.py:2067-2076`). That is not misdeal handling borrowed by
analogy — it is 35-E's *"play will stop and all bets will be returned"*, which
is the correct precedent and the one §3.5 adopts. What differs is that the void
is classified, recorded, announced, and attributed where the evidence allows,
none of which a fouled deck requires because a duplicate card accuses nobody.
What is missing is any *consequence* beyond the redeal, because there is no
eviction primitive
(B1) — and, per the paragraphs above, a seat repeatedly named by a void has not
thereby been shown to have cheated. A peer that voids every hand is a
denial-of-service the profile can classify but the protocol cannot yet answer.
Recorded in §9; it belongs to M2 and M7, not to a rules document.

---

## 7. Conformance status, and the gaps M7 owns

Read against `holdem/engine.py`, `holdem/p2p/replica_table.py`,
`holdem/client_view.py` and `holdem/contract.py` at `c69a4bf`. **Nothing was
changed.**

### 7.1 Conformant today

| Behaviour | Profile rule | Evidence |
|---|---|---|
| Heads-up button, blinds, act order, dealt last | §3.2, R34-B | `engine.py:471-473`, `609-617`, `830` |
| Odd chip walks forward from the button | §3.1, R20-A | `engine.py:969-977` |
| A short all-in does not reopen betting for prior actors | §3.3, R47-A first clause | `engine.py:758-776` |
| The minimum raise is the largest **increment** of the round, not the largest total | §3.3, R43-A + addendum | `engine.py:756-765` |
| A short all-in does not lower the minimum raise | §3.3, R47-A second clause | `engine.py:764-765`, updated only when `full` |
| Cards speak; settlement scores actual cards | §3.1, R12 | `engine.py:910-915` |
| Showdown order | §3.1, R17-A | `engine.py:984-995` |
| Uncalled bet returned to a single top live total only | §3.1, R15-B / R65-A | `engine.py:861-877` |
| Dead money never returns to a stack | §3.1, R30 | `engine.py:861-863` |
| Side pots layered by matched contribution, identical-eligibility merge | §3.1, R21 | `engine.py:917-932` |
| Committed chips stay committed | §3.3, R50-A | `engine.py:644-650` |
| Big-blind ante posted after the blind (big-blind-first) | §3.2, RP-11 | `engine.py:569-571`, `621-642` |
| One board per hand on the P2P path | §3.1, R38 / R39 | `replica_table.py:29-31` pins `runs=1` |
| Hidden information during play | §4.1 | `contract.py:46-85`, `mental_deal.py:741-762` |
| No burn cards | §3.2, R38 | `deal_map.py:13` |
| Fold-win reveals nothing at the client | §4.2 D-M1-1a | `client_view.py:129-142` |
| No straddle on the P2P path (by omission, not refusal — C5) | §3.2, R51-B | `replica_table.py:111` |

### 7.2 Gaps — all owned by M7, none fixed here

| ID | Gap | Profile rule violated | Site |
|---|---|---|---|
| **B6** | cumulative short all-ins do not reopen betting; each all-in is judged alone against `min_raise` and never accumulated | §3.3, Rule 47 | `engine.py:756-776` |
| **C1** | at a contested showdown the client renders the hole cards of seats that **folded earlier**, not only the seats that reached showdown | §4.2 D-M1-1c | `client_view.py:129-142` |
| **C2** | a sub-minimum raise is silently coerced up to `min_to` instead of being rejected. Deterministic, so replicas converge — and it is close to TDA's own correction remedy (52-A), which is why the gap is a **decision** rather than a defect. The profile still says reject: a repair rule is safe for a human at a table, not for arbitrary bytes from a hostile peer | §3.3, Rules 43-A and 52-A | `engine.py:747-750` |
| **C3** | `Engine.act(i, ...)` never checks `i == self.actor`. The replica layer gates it today, so the P2P path is safe; the engine itself is not | §3.3, Rule 53 | `engine.py:709`, gated at `replica_table.py:176-177` |
| **C4** | `contract.apply_command` sends the action string `"check"`, which `Engine.act` does not name. It matches no branch, so it silently discards the actor from `need_to_act` and advances the turn with no event and no `last_action`. Single-process path only — the P2P client sends `"call"` (`client_view.py:246-248`) | §3.3 "one canonical typed action, validated once" | `contract.py:106-107` vs `engine.py:713-747` |
| **C5** | nothing *asserts* that a P2P hand cannot contain a straddle. §3.2's ruling holds today only because `ReplicaTable.start_hand` omits `straddle_fn` — an omission, not a refusal. A future caller could pass one and no test would object, and straddle enablement is not a bound table parameter, so two replicas could disagree about it. Sharpening the point: the lobby already forwards a `straddles` flag from the stored table settings into the `game_start` payload (`onboarding.py:842`), and `ReplicaTable` has no parameter that could receive it (`replica_table.py:70-83`). The flag is inert, which is why the ruling holds — and nothing tells a host who set it that it was ignored | §3.2 | `replica_table.py:111`, `replica_table.py:70-83`, `engine.py:585-600`, `onboarding.py:842` |

C4 is new in this document; it was not in the research note. It is the same
class as C2 — an action outside the closed set is absorbed rather than refused
— and it is the reason the "one canonical typed action" invariant needs a test
rather than a sentence.

C5 has now changed meaning twice, and the second change is the substantive one.
It began as "straddle behaviour is unverified against TDA provisions". The
previous revision classified the straddle as a house ruling, because no
provision could be shown here. With the source pinned, §3.2 rules it out on
Rule 51-B — the posted big blind *is* the pre-flop opener — so the exclusion is
now an adoption of the professional blind structure rather than a preference of
ours. What remains under C5 is purely conformance: the ruling is true of the
code today and nothing holds it there.

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
| straddle enablement | **no — and not offered** | §3.2 rules that no P2P hand contains a straddle, so there is nothing to bind. The `straddles` flag the lobby already ships inside `table_settings` (`onboarding.py:842`) is **not** a binding of this field and must not be read as one — nothing consumes it (C5). If straddles are ever enabled here it becomes consensus-critical the moment they are, and must be bound in the same pre-image: replicas that disagree about it differ in blinds, in `min_raise` and in the first actor |
| big-blind ante order | **no — fixed by this profile** | not a parameter: §3.2 adopts RP-11's big-blind-first calculation for every table. It is listed because it is consensus-critical and someone will eventually want the alternative (ante-first) as an option; providing it would be a ruling change and a revision bump, not a table option |

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
* **A seat named by a void still gets a redeal**, and being named is not a
  finding that it cheated (§6). Void → redeal is the only remedy available,
  because membership cannot shrink mid-hand (B1). A peer that voids every hand
  is a denial of service this document can classify but not answer. M2 owns
  suspension; any exclusion policy is later still.
* **Two rulings here are ours, not TDA's** — the rows marked **[H]** in §3:
  the allocation of dead money that sits above every live stake (§3.1) and
  timeout while cryptographically absent (§3.4). Each names the provisions
  searched. Finding published text that contradicts one is a ruling change and
  a revision bump (§1.3); text that confirms one only refines the citation.
* **The classification rests on an extraction, not on the official PDF.** The
  pinned source is a mechanical text extraction of the official 2024 v1.0
  DOCX, with the DOCX and PDF SHA-256 hashes recorded alongside it. Paragraph
  and table order is preserved, but layout is not, so where exact layout could
  change a reading the provenance note directs a reader to the official PDF.
  No ruling in §3 turns on layout; every [V] citation is a sentence.
* **Rabbit hunting cannot be prevented, only left undisplayed** (§3.1, §4.2b).
  Rule 28 is the one professional rule this protocol structurally breaks rather
  than adapts.
* **RP-11 is advisory and this profile makes it binding** (§3.2). TDA
  *recommends* the big-blind-first calculation; consensus requires a single
  answer, so here it is a rule. That difference in force is ours.
* **The straddle is classified but unenforced** (C5). §3.2 rules that no
  profile-governed hand may contain one; today that holds only because
  `ReplicaTable` never passes `straddle_fn`. M7 owns the control.
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
* **The pinned source is append-only in practice: do not re-extract or edit
  `poker-tda-2024-v1.0-longform.txt` in place.** Every `src:NNN` citation in
  this document points at a line of that exact file. The **rule number is the
  authority and the line is a convenience**, so a shifted line makes a citation
  inconvenient rather than wrong — but a regenerated extraction silently shifts
  hundreds of them at once. If the file must be replaced, re-verify every
  citation in §3 in the same change, and check the new text against the DOCX and
  PDF SHA-256 in the provenance note first. Replacing it with a *different
  edition* is not a source update at all: that is a new profile identifier
  (§1.3).
