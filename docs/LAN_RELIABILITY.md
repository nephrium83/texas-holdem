# Casual LAN reliability

This implements the approved rules in `CASUAL_P2P_RULES.md` for fun games.
Reconnect, host migration, cash play, and the future recovery journal remain
outside this profile.

## Player behavior

- Turns show a 30-second countdown. At expiry the player's own app checks
  if legal, otherwise folds through the ordinary signed action.
- Next Hand starts automatically after 30 seconds. Players can select it
  sooner. Result agreement is required before advancing.
- The screen names players whose shuffle, decryption, audit, or result
  messages are outstanding. Missing automatic work expires after 30 seconds;
  a missing betting action expires after 45 seconds, allowing 15 seconds
  after the player's own turn clock.
- A non-host departure stops the hand. Each survivor independently applies
  the approved casual forfeit/cancel rule and compares signed results before
  continuing. The departed player's uncommitted chips remain theirs and
  their seat is excluded from future deals.
- Losing the host closes the table and shows the last settled balances.

Before starting, every roster identity must supply a fresh signed clock
sample for that roster and profile `casual-lan.reliability.v1`. The maximum
difference between adjusted samples must be at most 20 seconds. Missing
samples prevent an older incompatible app from starting. A wall-clock jump
greater than 30 seconds relative to monotonic elapsed time during play
causes a clock departure.

## Protocol and ownership

The LAN adapter explicitly enables this profile. Other embedders do not
silently inherit its scheduler. One adapter-owned ticker invokes serialized
Session operations; the renderer displays `turn.clock` without deciding
timeouts. Legacy `timeout_proposal` messages remain refused.

The host is timekeeper while participating, including while folded or
all-in. Once busted, the lowest numbered active player is timekeeper;
the host still relays. A timeout observation names exact public protocol
work and is signed by the elected timekeeper. Receivers validate identity
and scope without comparing their own elapsed clocks.

Seat numbers, signing-key bindings, and the cryptographic context stay
fixed. A departure set excludes retired seats without deleting balances.
A signed boundary covers hand/action position, membership, the reserved
continuation hand, balances, and blind/button positions. Surviving
participants independently compute and acknowledge identical results.
Additional departures are serialized. Disagreement closes the table
visibly and retains each app's own result.

Continuation reserves a protocol hand number beyond any immediately ahead
deal. Hand numbers may skip after departures; abandoned key ceremonies
are never restarted with reused hand inputs.

The host/timekeeper is trusted for casual timing and loss observations;
elapsed time on another machine is not independently verifiable. Mandatory
Bayer-Groth proofs and signatures remain. The rules-profile/context binding
redesign remains deferred.

## Verification

Fake-clock and signed multi-session tests cover expiry boundaries, public
missing-contribution projections, stale scopes, clock agreement, result
barriers, fixed seats, conserved chips, and crossed-action disputes.
Renderer tests cover clocks and readiness. Packaged acceptance requires
three independent players, ten completed hands, an actual 30-second stall,
automatic Next Hand, a non-host departure, matching survivor balances,
and continued play. Evidence belongs in the PR and delivery handoff.
