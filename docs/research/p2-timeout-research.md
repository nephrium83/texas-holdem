# P2 Timeout Semantics — Research

**Type:** Research / candidate design. **Not** a normative protocol
requirement. `docs/TIMEOUT_SPEC.md` remains the (currently incorrect)
contract until it is deliberately revised.
**Date:** 2026-08-18
**Base:** `07f61a7` (origin/main)
**Method:** abstract models with independent per-seat clock skew and
per-link delay. No repository code was modified or executed as part of
the candidate design.

## Established baseline (defects in the parked artifacts)

Reproduced against `6b553a8` (P2c):

- **A** — a hostile canonical betting token at `t=0` is accepted: the
  victim's state digest changes with no time having elapsed.
- **B / C** — `ACTION(N)` and `TIMEOUT(N)` delivered in opposite orders
  produce different digests at 2 and at 3 seats.

## Finding D — the general result

> Any final-outcome rule that compares a **receiver-local clock** against
> a threshold can split honest replicas at that boundary.

Demonstrated: one signed action landing at local elapsed 19.9 s and
20.1 s at two honest replicas produced `action` at one and `void` at the
other. This is not a tuning problem; it is structural.

Consequences:

- The outcome must be a deterministic function of the **signed evidence
  set**.
- Local clocks may gate **when this peer emits** evidence and **when it
  commits locally for display**. They must never change how a received
  message is interpreted.
- This also kills the intuitive "soft deadline at T, hard local cutoff at
  T+G" design, which splits identically at `T+G`.

## Semantics compared

Four candidates were modelled under controls A–N.

| | VOID | TIMEOUT-WINS | HARD-CUTOFF | CERTIFICATE (E=T+G) |
|---|---|---|---|---|
| hostile timeout at t=0 (n≥3) | pass | pass | pass | pass |
| opposite arrival order | pass | pass | pass | pass |
| manufactured ambiguity | **fail** | pass | pass | pass |
| boundary straddle (D) | **fail** | **fail** | pass | pass |
| local cutoff straddle | pass | pass | **fail** | pass |
| honest actor uses full advertised clock | **fail** | **fail** | pass | pass |
| heads-up hostile opponent | pass | pass | pass | **fail** |

`VOID` also fails the non-profitability invariant directly: an actor who
deliberately acts at the deadline manufactures a contest and recovers its
pre-hand stack — a free mulligan restricted to the last `G` seconds
rather than removed.

## Candidate: certificate semantics (n ≥ 3)

A betting `TIMEOUT(N)` applies only with signatures from **every** live
non-actor seat in the frozen participant set. An honest non-actor signs
only once its own elapsed reaches `E`, and **never after observing a
valid `ACTION(N)`**. That second clause does the security work: one
honest peer holding the action prevents any certificate forming.

Outcome function, containing no clock comparison:

```
certificate exists  -> deterministic default action (fold if facing a bet, else check)
else action exists  -> the action
else                -> unresolved
```

### Parameters

```
T = 30 s   advertised decision period   (matches TDA 2024 Rule 29: 25 + 5)
Δ = 5 s    tolerated honest timer-start skew
L = 5 s    tolerated honest propagation/processing delay
G = 10 s   DERIVED, = L + Δ             — never transmitted independently
E = 40 s   peer signing threshold, = T + G
```

`E = T + G` is the smallest signing threshold under which the advertised
30-second deadline is actually honoured. At `E = T`, an honest player who
used the full displayed clock is folded anyway. The 30–40 s window is a
propagation guard, **not** extra thinking time.

Δ and L are deliberately conservative for a first internet-capable
version. Real inter-continental RTTs are far smaller; the budget absorbs
poor Wi-Fi, scheduling stalls and transient congestion. Choosing Δ too
small is the dangerous direction — it falsely labels an honest signed
timeout as malicious.

### Residual fairness leak, stated exactly

An action is guaranteed honoured below `x = E − G = 30` and guaranteed
rejected at or above `x = E + Δ = 45`. Between them the outcome depends
on actual delays, so a modified client may sometimes gain up to
**`L + 2Δ` = 15 s** — not `G`. This is a fairness cost, not a safety one:
every replica still converges, and the gain is non-deterministic for the
cheat. Narrowing it means measuring Δ and L down, which is deployment
work.

## Heads-up (n = 2) — a genuine impossibility

With one non-actor, a "certificate" is one signature: the opponent's.
That is unilateral authority over another player's chips, not Byzantine
evidence. Modelled and confirmed: a lone opponent forms a complete
certificate.

| rule | hostile opponent forces early fold | hostile actor gets a mulligan |
|---|---|---|
| cert wins | **yes** | no |
| actor may veto | no | **yes** |

No third signature exists to break the tie, so n = 2 tolerates f = 0.
**Accepted posture: the n = 2 branch never evaluates a certificate at
all.** Silence produces a suspension, not a fold. A malicious opponent
may stall the table but may not steal a hand by asserting that the actor
timed out.

A victim's local clock reading is **not** independently verifiable
protocol evidence. It may be logged; it must not be described as
attributable.

A future heads-up mechanism may use a narrow independent timing/liveness
witness. Such a witness must not learn hole cards, participate in
shuffling, hold key material, or determine winners. Out of scope; recorded
as a requirement.

## Controls E–L (certificate candidate, modelled)

| Control | Result |
|---|---|
| E · a required signer holds the action first | suppresses; no certificate; outcome `action` |
| F · full certificate, no action | deterministic default action |
| G · all delivery permutations | 6 orderings, 1 outcome |
| H · stale/future seq, wrong hand/actor/phase/profile/params | all 7 rejected, 0 signatures accumulated |
| I · attacker redefines "live seats" | no certificate without the frozen set's full signatures |
| J · confederate withholds | suspends; no fold, **no void, no refund** |
| K · heads-up | lone opponent forms a certificate — the authority n=2 refuses to grant |
| L · committed chips on disappearance | 820 stack, 180 in pot |

## Superseded by the dropout audit

An earlier draft resolved the confederate-withholding case with
certificate-gated **eviction** (evicted seats fold, quorum shrinks). The
dropout audit shows membership cannot shrink mid-hand — `MentalDeal` is
n-of-n and `deal_map` is keyed on `seats_in`. Eviction is unavailable and
that escape is withdrawn. Frozen membership is now a property the
cryptography enforces rather than a policy choice, which is what
control I rests on.

See `docs/research/p2-dropout-audit.md`.
