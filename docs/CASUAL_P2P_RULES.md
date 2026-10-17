# Casual P2P rules

Section 3 of the P2P casual plan (`P2P_CASUAL_PLAN.md`, prepared
2026-10-04), which Adam approved on 2026-10-04 with all seven of its
decisions. The text below is copied from that section unchanged. Its file
and line references point at `origin/main` at `c1a2cf2` unless a branch is
named ("B2" is the `feat/beta-b2-session` branch). The decision that
adopts these rules for casual tables is the "casual P2P profile" row of
2026-10-04 in `docs/ROADMAP.md`.

**The rules, as players see them**

1. **If you drop during a hand, you fold.** Crashing, disconnecting, being disconnected and going silent past a timer all count. Every chip you put in stays in the pot, including a raise nobody has called yet.
2. **If that leaves one player in the hand, they win the pot.** No cards are shown.
3. **If two or more players are left, the hand stops and is split.**
   - Each pot is shared equally among the remaining players who are in it, as if they tied. Odd chips follow the normal rule.
   - When everyone left had put in the same amount, this is exactly "everyone gets their own chips back and splits the dropper's chips".
   - It also happens if a player who already folded drops, because the remaining cards still need every player's key.
4. **Being all-in does not protect you.** An all-in player who drops forfeits too, because their cards cannot be opened without them.
5. **Leaving between hands costs nothing.** A hand starts when its cards are dealt. A drop while the next hand is still being shuffled cancels that hand: blinds go back, and everyone keeps their last settled stack. Nobody has seen a card, so this gains the leaver nothing.
6. **If the host drops, the table ends for everyone with "connection to host lost".** The host is the player whose game relays everyone's messages. Every screen shows the last settled stacks and says the hand in progress was not settled. Nobody is credited with the host's chips: a joiner cannot tell a host crash from losing its own connection (session.py:2852-2858), and joiners have no link to each other to compare results.
7. **Timers.**
   - **Your turn:** 30 s. Then your game checks if it can and folds if it cannot.
   - **Next Hand:** if you have not pressed it 30 s after a hand ends, your game presses it for you.
   - **Automatic steps** (shuffling, unlocking cards, the end-of-hand card check, the settlement report) take an honest game a second or two. If the table waits on one player for more than 30 s for one of them, or 15 s past their turn timer, that player is treated as dropped.
   - The screen always shows who the table is waiting for, with a countdown, so nobody has to leave to break a stall.
8. **Heartbeat.** Every game sends a tiny "still here" signal every 5 s. If the host hears nothing from a player for 20 s, that player is dropped. If a player hears nothing from the host for 20 s, the host is lost (rule 6). This catches crashes, sleeping laptops and Wi-Fi drops that leave no closed connection behind. A blip shorter than 20 s costs nothing.
9. **The timekeeper.** The host runs the timers in rule 7. Once the host has busted out, the lowest-numbered player still playing takes over the timers; the host still relays and still runs the heartbeat. Every timeout drop is a signed notice that names who sent it.
10. **No reconnect in v1.** Once you drop, you are out of that table.
11. **If the players' games disagree about a result, the table ends.** Each player sees their own game's result, marked "disputed". Nothing is rolled back.
12. **Clocks.** Before the first hand, every game compares its clock with every other player's. If any two are more than 20 s apart, the game does not start and says whose clock is off; nobody loses chips. During play, a player whose clock jumps by more than 30 s can no longer be heard and is treated as dropped, with the reason "clock" shown. If the host's clock jumps, rule 6 applies.

**How every copy of the table stays in step**
- **No figures come from the host.** A drop notice names the dropped seat, the hand number, whether the hand had been dealt, and the sender's action count. Every game still playing works out the forfeit from its own copy of the table.
- **Every game follows the notice.** Everything the notice's sender had seen reaches every other game first, because all messages pass through the host in order. So the notice's hand and "dealt or not" are safe for everyone to follow. A game that already began a later, undealt hand cancels it (rule 5).
- **The one race fails visibly.** A game that applied its own action in the same instant as the drop has an action count above the notice's. It shows its result marked "disputed" (rule 11) instead of guessing.
- **A busted host keeps B2's path.** It stops tracking hands (B2 session.py:2350-2360; 3153-3156), so it only reports the drop, and a seat still playing confirms it with the hand and action count (B2 commits fcdee0e, 3a41711). The same seat is the timekeeper.
- **Settling without the end-of-hand card check (the "audit") requires proofs.** The drop path itself checks that the table runs Bayer-Groth and that this game verified every shuffle round (session.py:1012). Today that rule is tied to the transport type (session.py:399-400, 1097-1101); the direct check removes that dependence.

**Why rule 3 splits pot by pot**
- It reuses the engine's existing side-pot layers and odd-chip rule (engine.py:917-938, 969-977).
- A literal "own contribution back plus an equal split" can overpay a short all-in player. Someone all-in for 10 chips would get 510 instead of 15.
- The engine's normal settle() returns an uncalled raise to its owner even when that player has folded (engine.py:861-877). A probe paid a dropper back 190 of a 200 raise. So the forfeit needs its own small function.

**ROADMAP standing invariants**

| Invariant | Status |
|---|---|
| 1. Non-profitability | Kept for players. Exceptions: a host drop cancels the hand in progress (rule 6), and the cancel paths under "accepted gaps" below. Nothing outlives one table, so a cancelled hand saves the canceller no chips it can use later. |
| 2. Every seat stays a required crypto participant | Relaxed for drop-settled hands (decision 2). For casual tables this supersedes the REFUTED sole-live-player note (ROADMAP.md:200-208). Its five revival conditions are in the next table. |
| 3. Evidence decides, clocks do not | Kept, with one named exception: the ±30 s message window (wire.py:190-195). Drops apply at the position in the signed notice. Clocks only decide when this player's own game acts, when the timekeeper sends a notice, and whether a message is inside the window. The start-of-table check (rule 12) keeps honest drift inside it. |
| 4. Committed chips stay committed | Kept for the dropper: this replaces B2's refund. Relaxed for the players left: rule 3's split hands back their own chips, and rule 5 hands back blinds of an undealt hand. |
| 5. A crypto failure is not a misdeal | Unchanged. |
| 6. Heads-up loses liveness, not safety | Relaxed: heads-up, a timeout drop is a forced fold. Accepted as timekeeper trust. |
| 7. Bayer-Groth proofs are mandatory | Kept, and the drop path now checks it directly. |

**The five revival conditions** (p2-suspension-reconnect.md:202-209), for settling a hand without the audit:

| Condition | Status |
|---|---|
| 1. Bayer-Groth required, whatever the transport | Met by increment 3: the drop path checks it directly, not the transport type. |
| 2. The settling game verified every shuffle round itself | Met. A round without a valid proof aborts the deal (mental_deal.py:624-627), and increment 3 also checks the count. |
| 3. The player who disappeared is never paid | Met, with one waiver (decision 1): a sole winner who drops during the audit is paid. He may by then have seen the folded players' cards without showing his own. |
| 4. No folds made by a timeout | Not met: rule 7 makes timeout drops. Accepted as timekeeper trust. |
| 5. Shuffle decks and received audit shares are kept for a later audit | Partly met. Today they live only in memory (mental_deal.py:232, 632). The table log (increment 20) keeps them for drop-settled hands; nothing re-checks them automatically. |

**Accepted gaps (play money only)**
- **Collusion.** A dropper's friend still in the hand splits the dropper's chips instead of losing them at showdown. A friend who already folded can drop to force a split.
- **Host and timekeeper trust.** Only the host sees a joiner disconnect (TOPOLOGY_DECISION.md), and the host can cut anyone off (transport.py:788-794). The timekeeper could time a player out early. A modified host or timekeeper could cut someone off and collect; heads-up, it wins the pot. Mitigation: every drop notice is signed and names its sender, and the screen says "the host disconnected you" or "the timekeeper timed you out", which differs from "you lost connection".
- **All-in droppers lose.** The "knock out the all-in leader" risk (MULTIPLAYER.md:113-118) stays open. Threshold keys are the long-term fix.
- **Host loss ends the table** and cancels the hand in progress (rule 6). There is no host migration.
- **Ways a hand can still be cancelled.** A modified client can void the hand in progress and get its chips back in four ways: a hand_void message (session.py:1848-1861), a bad card-unlock share (session.py:2309-2313), a wrong state check attached to an action (session.py:2196-2203), or two different messages under one sequence number (session.py:1247-1252). Honest games never do these. Closing them later: a void that blames a seat (bad share, double message) becomes that seat's drop; a void that blames nobody (hand_void, a state mismatch) ends the table as "disputed".
- **Stakes are one table.** The sidecar keeps no bankroll; only the Tk GUI does (settings.py:103-105).

**Existing "undo buttons" this plan closes**
- **Time-zero timeout proposal.** A seat can send a timeout proposal at time zero and void the hand with a refund (session.py:3317-3352; a probe confirmed it). No honest client sends one, because check_deadlines has no production caller. Fix: refuse it.
- **Lying settlement report on B2.** A seat whose first settlement report lies rolls the table back to the start of that hand (B2 session.py:2068). Fix: rule 11.
- **Silent clock disconnect.** A message more than 30 s off drops the whole connection with no reason shown (wire.py:190-195; transport.py:467-475). Fix: rule 12's start check, and the reason is shown.
