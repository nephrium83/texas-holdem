# Professional Poker Rules Audit

**Type:** Research / proposed classification. **Not** yet a normative
profile — `docs/POKER_RULES_PROFILE.md` does not exist and is a roadmap
deliverable.
**Date:** 2026-08-18
**Base:** `07f61a7` (origin/main)

## Source hierarchy and versions

| Source | Version | Status | Used for |
|---|---|---|---|
| Poker TDA Rules | **2024** | current published | primary normative base |
| WSOP | 2026 Main Event | cross-check | clock policy |
| GGPoker house rules | current | cross-check | online disconnect / timeout defaults |

**Version pinning matters.** TDA Summit XII met 29–30 June 2026, but no
2026 ruleset is published. The proposed profile identifier is therefore
`poker.tda.2024.nlhe.v1`. Do not adopt a 2026 identifier until TDA
publishes one.

WSOP 2026 introduced a 20-second action clock with six 30-second
extension chips on Day 7 of the Main Event, then removed it for the final
table — confirming clock duration is an event-level policy rather than a
fixed rule.

## The three citations that decide our design

**TDA 2024 Rule 29 (Calling for a Clock)** — "A player on the clock has
up to 25 seconds plus a 5 second countdown to act. If the player faces a
bet and time expires, the hand is dead; if not facing a bet, the hand is
checked."

This verifies both `T = 30 s` and the fold/check split. Neither was
invented: 25 + 5 is the professional figure.

**TDA 2024 Rule 30 (At Your Seat and Live Hands)** — players not at their
seats have their cards killed immediately, and their posted blinds
"forfeit to the pot."

This is the professional precedent for the accepted disconnect
economics: absence forfeits the current hand and the chips already
committed; it does not refund them.

**GGPoker house rules** — "If a player times out during a hand, whether
connected or disconnected, his or her hand will be folded if facing
action or may be checked if facing no action."

An independent operator confirming the same consequence, and explicitly
declining to distinguish connected from disconnected for the *poker*
outcome.

### The one refund precedent, and why it does not transfer

GGPoker does cancel games and return chips — but only on **server
crash**, i.e. failure of the trusted operator's own infrastructure, never
player silence. In a P2P table there is no operator, so the analogue must
be an event **no player can unilaterally trigger**. Any void-with-refund
a player can induce by going quiet is a disconnect mulligan.

## Proposed classification matrix

`ADOPT` · `DIGITALIZE` · `OVERRIDE` · `N/A`

| Area | Rule | Class | Digital form / reason |
|---|---|---|---|
| Cards speak | 12 | ADOPT | verified cards plus evaluator determine the result; no UI text alters settlement |
| All-in hands tabled | 16 | ADOPT | engine forces this via `force_tabled` |
| Showdown order | 17 | ADOPT | last river aggressor tables first, else first live seat left of button |
| Odd chips | 20 | ADOPT | first winner left of the button. **Consensus-critical** |
| Clock duration and default | 29 | DIGITALIZE | 25+5 becomes a deterministic `T = 30 s`. Consequence adopted verbatim; the *authority* to declare expiry is replaced |
| Absent player, blinds forfeit | 30 | ADOPT | encodes the non-profitability invariant |
| Dead / moving button | 34 | ADOPT | engine implements a forward-moving BB anchor |
| Heads-up button | 34-B | ADOPT | SB is the button, dealt last, acts first pre-flop and last thereafter |
| Misdeals, fouled decks | 35 | N/A | no physical dealing. Explicitly **not** the home for cryptographic failures |
| Substantial action | 36 | N/A | exists to bound correction of physical errors |
| Raise amounts | 43 | DIGITALIZE | the 50%-or-more clause is a human-gesture remedy. A sub-minimum raise is **rejected**, not rounded up |
| Oversized chip betting | 44 | N/A | no chips are pushed |
| Re-opening the bet | 47 | ADOPT | including **cumulative** short all-ins — see engine gap below |
| Binding declarations, undercalls | 51 | N/A | one canonical typed action, accepted once |
| Action out of turn | 53 | DIGITALIZE | rejected before becoming game state; no backup, no binding, no penalty machinery |
| No maximum bet (NLHE) | — | ADOPT | bounded only by stack |
| String bets, exposed/burn cards, fouled deck, dealer gesture | various | N/A | whole class eliminated by typed actions and a cryptographic deck. `deal_map` already burns nothing |
| Run it twice | — | OVERRIDE | not a TDA tournament rule; a supported cash-game extension (odd chip to run 1) |
| Timeout while cryptographically absent | — | OVERRIDE | no professional analogue; suspension semantics |

## Existing-engine compliance

Read against `holdem/engine.py`. Nothing was changed.

| Behaviour | Verdict | Detail |
|---|---|---|
| Heads-up button, blinds, act order | compliant | three independent paths agree; SB is button, acts first pre-flop, last post-flop, dealt last card |
| Odd chip award | compliant | walks forward from the button to the first winner |
| Short all-in does not reopen betting | compliant | prior actors re-added to `need_to_act` but marked `no_raise` |
| **Cumulative short all-ins reopening betting** | **GAP** | Rule 47 says cumulative short all-ins totalling a full raise *do* reopen. The engine judges each all-in individually against `min_raise` and never accumulates |
| Sub-minimum raise handling | decision | `act()` silently coerces upward to `min_to`. Deterministic, so it converges — but a malformed action from a hostile peer still mutates state. Profile says reject |
| Turn enforcement | decision | `Engine.act(i, ...)` never checks `i == self.actor`; the replica layer gates it today |
| Cards speak / evaluator | compliant | settlement scores from actual cards |
| Showdown order and mucking | compliant | river aggressor first; a hand that cannot win may muck |
| Uncalled bet returned | compliant | only when a single player holds the top live total |
| Dead money never returned | compliant | antes and dead blinds stay in the pot, consistent with Rule 30 |
| Side pots | compliant | layered by committed total, identical-eligibility merge, residual dead money to the top pot |
| Straddle | unverified | engine supports a UTG straddle; not checked against TDA straddle provisions |

## Invariants this audit proposes

1. One canonical typed action, validated once, accepted once, never reinterpreted.
2. Out-of-turn actions rejected before becoming game state.
3. Sub-minimum raises rejected, not silently coerced.
4. Cards and evaluator determine settlement; no declaration channel exists.
5. Odd chips: first winner left of the button, walking forward.
6. Committed chips are never returned to a player who stops responding.
7. Dead money never returns to any stack.
8. Cumulative short all-ins reopen betting when they total a full raise.
9. Poker participation and cryptographic participation are different roles.

## Physical-casino rules to eliminate rather than emulate

String bets · oversized-chip ambiguity · verbal-versus-chip conflicts ·
undercalls · exposed cards · burn cards · fouled decks · dropped or
mucked physical cards · dealer gesture ambiguity · misdeal declaration ·
substantial-action windows · out-of-turn backup and binding.

Each exists because humans handle physical objects imprecisely.
Reproducing them would manufacture ambiguity the protocol has already
removed — and each would need its own consensus rule, since any ambiguity
resolved differently on two replicas is a state split.

## Cryptographic integrity failure is not a misdeal

A misdeal is a procedural error with no bad actor, which is why TDA can
say "once substantial action occurs the hand must proceed." An invalid
Bayer–Groth proof, a malformed ciphertext, a transcript mismatch or a
failed DLEQ is *evidence of attempted cheating*. "Play on" is correct for
the first and unacceptable for the second. `MentalDeal` already fails
closed and attributes; the profile must state that this category never
inherits misdeal remedies.

## Consensus-critical fields

Two peers must not begin a hand while silently using different poker
rules or timeout arithmetic. Proposed canonical binding, alongside the
deal policy and seat order already bound:

| Field | Bound | Note |
|---|---|---|
| `rules_profile` | yes | e.g. `poker.tda.2024.nlhe.v1` |
| `T`, `delta`, `L` | yes | timeout parameters |
| `G`, `E` | **no** | strictly derived (`G = L + Δ`, `E = T + G`); transmitting them would let peers disagree about values with no independent existence |
| `timeout_policy_version` | yes | so a future change is a clean wire break, not a silent split |
| `seats_in`, button | already bound | frozen participant set and deal map |
