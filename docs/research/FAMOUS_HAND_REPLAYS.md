# Famous poker hand replays

Research and executable acceptance evidence, 2026-10-19. Initial audit baseline:
`e504e2b0eac3985ba551a7d291f27d6499463dff`. The initial audit below made
no production or rules-contract changes. Adam subsequently approved the
standard WSOP ante correction; the current decision is recorded separately
below, with active rules profile `poker.tda.2024.nlhe.v2`.

Seven sourced hands are encoded in `tests/famous_hands/cases.json`. Run:

```powershell
python tools/replay_famous_hands.py --output build/famous-hands-results.json
python -m pytest tests/test_famous_hands.py -q
```

Each action is applied to two production `ReplicaTable` instances. The
harness verifies actor, street, exact call/check semantics, raise bounds,
chip deltas, state digests, board cards, winner, pot eligibility, refund,
payouts and ending stacks. It rejects historical bets the engine would
otherwise silently clamp. Its pot oracle layers live bets and puts dead
antes in the main pot, following the WSOP interpretation cited below.

Cards are supplied and human actions are scripted. This proves betting,
ranking and settlement behavior for the stated fixtures. It does not prove
bot strategy, the Godot controls, cryptographic dealing, network delivery,
or a verbatim replay of details that a broadcast never disclosed.

## Historical audit results before the ante correction

| Hand | Result | What was exercised | Reconstruction limit |
|---|---|---|---|
| [Moneymaker–Farha, 2003 bluff](https://www.pokernews.com/strategy/analyzing-moneymaker-bluff-of-the-century-against-farha-35218.htm) | PASS | Turn raise, river shove, king-high winning when a pair folds; uncalled shove returned | Individual antes omitted; published starting stacks are rounded |
| [Hansen–Negreanu, 2006](https://www.cardplayer.com/cardplayer-poker-magazines/65638-max-pescatori-3-10/articles/16208-the-gus-hand) | PASS | Set over set becomes quads over full house; two check-raises and called river shove | Compressed folded seats and no antes; normalized pot 574,900 versus reported 575,700 |
| [Weinman–Jones, 2023 WSOP final](https://www.pokernews.com/tours/wsop/2023-wsop/main-event/chips.590268.htm?busted=1) | ANTE RULE MISMATCH | Flop check-raise, turn all-in, river runout and kicker | Exact documented action; effective losing stack derived, covering stack assumed; existing low-level big-blind ante enabled |
| [Duhamel–Affleck, 2010](https://www.pokernews.com/tours/wsop/2010-wsop/main-event/post.161456.htm) | PASS | Four-bet, turn shove, river straight beating pocket aces | Individual antes removed from active starting stacks; folded seats compressed |
| [Rounders opening](https://www.mypokercoaching.com/teddy-kgb/) | PASS | Aces full beats nines full; legal short all-in raise | Movie card reconstruction, normalized blinds/stacks and final shove |
| [Rounders finale](https://www.pokernews.com/strategy/old-school-vs-new-school-negreanu-rounders-final-hand-39254.htm) | PASS | Three check-calls, overbets, all-in and ten-high straight | Preflop prefix and stacks reconstructed; Teddy's exact cards unknown |
| [Casino Royale finale](https://wizardofodds.com/newsletter/december-30-2021/) | PASS | Four-way all-in, short call, main pot plus two side pots; straight flush beats full houses and flush | Earlier action/stacks reconstructed; one eight's suit disputed; the published bet amounts imply 116m, not the film's spoken 115m |

Six adapted action replays passed. The seventh reached the correct real-life
winner but failed the standard-ante pot oracle. This distinction matters:
matching the winner alone would miss incorrect pot ownership.

The saved replays execute **86 scripted actions** across the seven fixtures.
The focused replay, replica and forfeit suite reports **83 passed, one
precisely scoped expected failure**. The separate exhaustive movie comparison
checks all 990 compatible hidden hands. Source/action and movie-assumption
reviews found no remaining issues after narrowing the known-mismatch guard.

The initial pytest suite recorded only this precisely reproduced ante
discrepancy as a strict expected failure. Other errors still failed. The
initial audit command returned nonzero and recorded `RULE_MISMATCH`,
including actual and expected pots and the complete action trace. That
checkpoint's CI success did not mean seven successful historical replays.
The correction removes the expected-failure marker and special mismatch guard.

## Historical finding: ante discrepancy and the v1 contract

[WSOP's own big-blind ante explanation](https://www.wsop.com/news/big-blind-ante-coming-to-2018-wsop/)
makes the entire ante available even to a short-stack winner. It is dead
money in the main pot, rather than an additional live bet the poster alone
can win. The 2023 final really used a big-blind ante, also confirmed by
[PokerGO's event report](https://www.pgt.com/news/daniel-weinman-wins-2023-wsop-main-event-for-12100000).

A small reachable control exposes the issue: both players commit 100 live
chips and the big blind also posts a 20 ante. Aces beat kings on a dry board.
Standard expected stacks are `[220, 0]`; the v1 engine paid `[200, 20]`.
It created a 200 main pot plus a 20 pot eligible only to the ante poster.
The forfeit helper exhibited the same underlying eligibility convention.

The then-active `docs/POKER_RULES_PROFILE.md` v1 explicitly adopted
total-contribution layering and a house rule for residual dead money. Existing
forfeit tests also explicitly expected a private dead-money layer. Consequently
this was a confirmed mismatch with WSOP rules, but changing it was a rules
decision rather than an unnoticed implementation typo. No production
settlement code was changed during the initial audit.

The audit recommended this correction: layer only matched live
bets for side pots, put all dead ante money in the shared main pot, preserve
uncalled live-bet refunds, and preserve the distinct casual rule forfeiting
an unmatched raise by a dropped player. Update the versioned rules contract
and cover both normal and forfeit settlement. This requires no new transport,
shuffle, native runtime, scheduler or wire redesign. Adam approved that
recommendation on 2026-10-19, superseding the old rule as recorded next.

The current `ReplicaTable` constructor and desktop table setup also do not
expose ante configuration. The WSOP fixture uses the already existing
`Engine.bb_ante` setting before startup; it is not a claim that the preview
can configure and launch that tournament from its menu. Individual-player
antes are not exposed through this path, so the older tournament fixtures
are explicitly adaptations.

## Approved correction, 2026-10-19

Adam selected **"Use standard WSOP antes (Recommended)"** after reviewing
the 100 + 100 + 20 example. The active contract is now
[`poker.tda.2024.nlhe.v2`](../POKER_RULES_PROFILE.md); the unchanged
[v1 contract](POKER_RULES_PROFILE_V1.md) remains historical reference.

Dead ante money belongs to the shared main pot and is available to every
non-folded contestant regardless of stack or whether its own contribution
is entirely dead. Live wagers alone determine side-pot thresholds. The
same main-pot allocation applies to other `total_dead` posts, including
dead small blinds; that extension is our ruling, not an assertion about
WSOP's ante article. Normal and forfeit settlement must agree on this
allocation. Blind-first ante posting and refunds of uncalled live bets
remain unchanged. The casual dropper's unmatched **live** raise remains
forfeited to the highest matched live pot rather than being reclassified
as ante money.

The rules identifier revision changes its final ASCII byte from `31` to
`32`. There is no runtime wire or cryptographic domain change because
the identifier is not currently bound into the deal context. This work
does not add ante configuration to the desktop menu or change dealing,
networking or bot strategy.

**Acceptance status:** all seven replays pass after the correction, including
all 86 scripted actions and the 345,500,000-chip WSOP final pot. The focused
ante, forfeit and replay suites pass all 79 tests. Thirteen new ante tests
cover short stacks, zero-live contestants, dead-only pots, live refunds,
side pots, ties, two boards and drop forfeits. A control loads the unchanged
v1 engine from `57b0990` and runs the 13 ante tests plus seven replays:
12 fail and eight pass, including a failure for the WSOP final and the
100 + 100 + 20 payout. Restoring the corrected engine makes them pass.
Full regression and packaged Windows results are recorded with the delivery
artifacts and pull request checks. The initial six-pass/one-mismatch results
above remain historical evidence.

## Movie uncertainty is preserved

Teddy's finale cards are not revealed. `Ah Ac` in the action fixture is an
illustrative opponent hand, not a film assertion. An additional test checks
all **990** compatible two-card holdings: Mike's straight wins 981, ties
nine other 8–9 combinations, and loses to none. No missing cards are needed
to establish that he has the nuts.

The Casino Royale fixture uses the published river bets and conserves
116,000,000 chips, with pots **44,000,000 / 3,000,000 / 69,000,000**. Bond
wins all three. Matching a claimed 115,000,000 instead would require an
additional assumption, such as a shorter Le Chiffre stack and an uncalled
return. The fixture does not silently remove a million. Infante's second
eight varies between accounts; both possible suits produce the same full
house. [Card reconstruction](https://www.thejamesbonddossier.com/features/the-final-poker-hand-of-casino-royale.htm),
[official Bond-channel clip](https://www.youtube.com/watch?v=3jQbXuvGR5o).

Other broadcast cross-checks: [Rounders opening clip](https://www.youtube.com/watch?v=MbrO-ZbuhIs),
[Rounders final-hand clip](https://www.youtube.com/watch?v=LIXJaTQTDAg),
[Hansen/Negreanu rewatch](https://www.cardplayer.com/poker-news/1640563-daniel-negreanu-gus-hansen-break-down-iconic-high-stakes-poker-hand),
[contemporaneous Affleck stack/betting report](https://www.cardplayer.com/hand-matchups/1284),
[Farha interview](https://www.pokernews.com/news/2017/11/unlit-cigarette-catching-up-with-2003-main-event-runner-up-s-29331.htm).

No blind appeal to a site's title was used: recent search results that
conflated Moneymaker's king-high bluff with his separate winning two-pair
hand were rejected in favor of consistent tournament/broadcast reporting.
