# MentalDeal Dropout Audit

**Type:** Research / evidence. **Not** a normative protocol requirement.
**Date:** 2026-08-18
**Base:** `07f61a7` (origin/main)
**Method:** staged silencing of one seat against the real `MentalDeal`
coordinator in a 4-seat in-process broadcast harness. Read-only; no
repository code was modified.

## Question

Can an original participant that disconnects (and is folded from poker
action) disappear permanently while the surviving seats finish the hand
cryptographically?

## Result

**No, at every stage from keygen onward — with one exception (below).**

`MentalDeal` is fixed-membership and n-of-n. Every phase gate compares
against the full `seats_in` list, and `elgamal.joint_public_key` is an
additive sum with no threshold, no Shamir sharing and no Lagrange
reconstruction. `elgamal.joint_public_key` states this in its own
docstring: the joint key is the group sum "so that decryption requires
every corresponding secret share `x_i` to contribute a partial
decryption."

### Measured, per stage

| Stage | Phase | Survivors complete? | Gate |
|---|---|---|---|
| Keygen | `KEYGEN` | No | `_on_key_announce` gates on `all(s in _pubkeys for s in seats_in)`; `_finish_keygen` never runs |
| Shuffle | `SHUFFLE` | No | `_expected_shuffler(r) = seats_in[r-1]`; measured 0 of 4 rounds accepted |
| Deal, pre-hole | `DEAL` | No | `_try_complete` returns unless `len(have) == len(seats_in)`; measured 3 of 4. No survivor recovers **any** hole card, including its own |
| Betting, post-hole | `DEAL` | Yes | holes already recovered before the drop — the only surviving window |
| Flop / turn / river | `DEAL` | No | same n-of-n gate; measured 0 of 5 board slots after all three streets were revealed by survivors |
| Showdown | `DEAL`/`AUDIT` | No | `MentalDealDriver.all_hole_cards()` returns `None` without a clean audit |
| Audit | `AUDIT` | No | `_maybe_run_audit` returns early unless every seat opened; measured shares `[1,2,3]` of `[0,1,2,3]`; phase never reaches `DONE` |
| Settlement | — | Conditional | see exception |

A stalled seat produces **no abort and no timeout** — the coordinator
simply stops. There is no fail-closed path for absence, only for
detected misbehaviour.

### Second, independent blocker

`deal_map(button, seats_in)` derives every hole and board deck position
from `seats_in`. Shrinking that list to route around a missing
participant would remap the entire deal mid-hand. Membership cannot be
shrunk even if the threshold problem were solved, because the deal map
and the decryption set are currently the same list.

## The exception

`Engine.settle()` with `len(alive) == 1` awards the pot having consumed
**0 deck cards** and run **0 scoring passes** — verified against the real
engine. So when the position already resolves to exactly one poker-live
seat, no board recovery is required.

> **Status: UNPROVEN as a protocol claim.** "The engine can settle
> without cards" is not the same claim as "the protocol may safely skip
> its remaining cryptographic audit." Whether skipping the audit on an
> uncontested fold-win is safe — particularly in detection-only mode,
> where the Phase D multiset check is the only thing that catches a
> card-substituting shuffler — is an open question tracked in the
> roadmap. Do not implement this path until that proof lands.

## Recoverable vs lost

`derive_share` is `HKDF(master_secret, session|hand|seat)` and is
deterministic. Verified: a rebuilt instance on the same device re-derives
an identical `X_i`; a different device does not.

This restores the **secret share only**. It does not restore the accepted
shuffle chain, the current encrypted deck, collected deal shares,
recovered streets, audit state, replica betting state, author-sequence
bookkeeping, or the hand transcript. The three failure classes are
therefore distinct:

| Class | Master secret | Session/deal state | Recoverable? |
|---|---|---|---|
| Transport interruption, process alive | survives | survives in memory | Easiest |
| Process crash, same device | survives (on disk) | **lost entirely** | Requires durable state or authenticated transcript replay |
| Device / secret loss | lost | lost | `x_i` cannot be regenerated; the hand is cryptographically unrecoverable |

**Nothing in the P2P layer persists hand or session state.** The only
disk writes are `device_secret.py` and `identity.py`.

## Consequence for the timeout contract

A timeout certificate folds a peer that is *stalling but present* — such
a peer keeps contributing decryption shares, so the hand continues. It
does nothing for a peer that is *cryptographically absent*, because the
hand cannot proceed without the missing shares regardless of who folded.
Suspension and reconnection, not the certificate, are what make remote
play survivable.

## Do not "fix" this with a threshold scheme reflexively

Additive n-of-n is also the privacy boundary: no proper subset of players
can decrypt anything, and during normal play a hole card's owner
withholds its own share from everyone else. Any `t`-of-`n` replacement
lets a coalition of `t` players jointly decrypt ciphertexts they were
never meant to see. Buying dropout tolerance with a naive threshold
scheme sells collusion resistance to pay for it, and collusion is the
primary threat in real-money online poker.
