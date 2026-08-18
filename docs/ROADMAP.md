# Roadmap

Canonical milestone ordering for the P2 workstream and everything
downstream of it. This file is the source of truth for sequencing.
Where it disagrees with a chat transcript, an artifact, or a research
note, **this file wins**.

- **Coordination issue:** see `docs/COLLABORATION.md`
- **Base at time of writing:** `07f61a7` (origin/main)
- **Research inputs:** `docs/research/` — dropout audit, timeout semantics,
  professional rules audit, suspension/reconnect

---

## Repository state

| Ref | SHA | Status |
|---|---|---|
| origin/main | `07f61a7` | canonical |
| P2a — timeout phase ownership | `6955f4f` | **parked**, two tests red on purpose |
| P2c — proposal applicability | `6b553a8` | **review artifact**, do not merge |
| PR #36 — production deadline ticker | — | **closed / unmerged**, forensic only |

> Local `main` may lag `origin/main`. At time of writing local `main`
> was `358a8e7`, an ancestor of `07f61a7`. Always branch from
> `origin/main`.

---

## Standing invariants

These hold across every milestone. A change that violates one is wrong
regardless of how convenient it is.

1. **Non-profitability.** A player's silence, timeout, disconnect or
   eviction must never give that player a better chip outcome than
   continued legal play would have.
2. **Two distinct roles.** Poker participation and cryptographic
   participation are different. A seat may fold and stop making poker
   decisions, but every original seat remains a **required cryptographic
   participant** until the hand is cryptographically complete.
3. **Evidence decides, clocks do not.** The final replicated outcome must
   be a deterministic function of signed evidence. Local clocks may gate
   when *this* peer emits evidence and when it advances a local
   timer-driven step; they must never make identical signed evidence mean
   different things on different replicas.
4. **Committed chips stay committed.** Disconnection or eviction never
   refunds chips already in the pot. Uncommitted stack remains the
   player's.
5. **A cryptographic integrity failure is not a casino misdeal.**
   "Substantial action occurred, play on" is correct for procedural
   dealing errors and unacceptable after a proof, transcript or
   decryption failure.
6. **Heads-up loses liveness rather than safety.** With n = 2 there is no
   Byzantine-safe peer-only betting timeout.
7. **Bayer–Groth prevention is mandatory** for every production wire-mode
   P2P game.
8. **`author_seq` is anti-replay evidence, not a consensus ordering
   primitive.**
9. **Tests must prove the discriminating invariant** and include faithful
   deliberate-break controls. A test that passes against a broken
   implementation is a monument to a bug.

---

## Known architectural blockers

| ID | Blocker | Impact | Tracked in |
|---|---|---|---|
| **B1** | `MentalDeal` is fixed-membership and n-of-n at every phase | evict → fold → continue is impossible; a missing participant stalls the hand with no abort path | M2 |
| **B2** | Nothing in the P2P layer persists hand or session state | a process crash loses everything except the device secret and signing identity | M2 |
| **B3** | n = 2 has no peer-only Byzantine-safe betting timeout | heads-up silence must suspend, not fold | M3 |
| **B4** | P2c accepts a hostile canonical timeout at t=0, and diverges on action-vs-timeout arrival order | current timeout path is unsafe | M4 |
| **B5** | P2c ownership regression: `@owned` sits on the pure `_expected_timeout_token`, while the mutating `_maybe_start_deadline` is unguarded | thread-safety guarantee weakened | M5 |
| **B6** | Engine does not accumulate cumulative short all-ins for re-opening (TDA 47) | rules non-conformance | M7 |
| **B7** | `conn_id` is baked into the cryptographic domain: `_deal_context_bytes` hashes seat-order conn_id strings into `_deal_session_id()`, which is the HKDF `session_id` for `derive_share` and the PoP/BG contexts | a reconnecting peer derives a **different** `x_share`, announces a conflicting `X`, and the hand aborts **blaming the honest returning seat** | M2 |
| **B8** | `wire.unpack` enforces a hard ±30 s freshness window | stored envelopes are already expired; transcript replay is impossible without re-signing, which destroys the property that made the transcript trustworthy | M2 |
| **B9** | Honest re-sends are indistinguishable from equivocation: `wire.pack` stamps a fresh `ts`, so the same logical message re-sent has a different envelope hash, which `_author_seq_ok` reads as equivocation | voids the hand blaming the honest returning seat; needs no attacker | M2 |

---

## Defects in `main` (found incidentally, not yet fixed)

These exist on `07f61a7` now. None is a hypothetical.

| ID | Defect | Severity |
|---|---|---|
| **D1** | `_bind_seat_keys` accepts a **partial** map and is one-way. A peer disconnecting in the `start_game` → `start_p2p_hand` window freezes its seat with no key, permanently unauthorizable for the session | high |
| **D2** | `_on_player_info` has no lifecycle gate, so any room-code holder can admit a fresh identity mid-hand and mutate roster state during play (gains no seat) | medium |
| **D3** | `MentalDealDriver.all_hole_cards()` has no phase gate — returns every seat's hole cards whenever the audit passed, including fold-wins the UI hides | high |
| **D4** | Bayer–Groth is enforced only when `author_mode == AUTHOR_MODE_WIRE`; on compat tables a shuffler can broadcast 52 arbitrary encryptions with no proof | high (compat only) |

Evidence: `docs/research/p2-suspension-reconnect.md`.

---

## Milestones

### M1 — Professional poker rules profile

- **Status:** ready to start (research complete)
- **Depends on:** nothing — runs independently of the P2 crypto work
- **Goal:** create `docs/POKER_RULES_PROFILE.md` pinned to
  `poker.tda.2024.nlhe.v1`, classifying every relevant rule as
  ADOPT / DIGITALIZE / OVERRIDE / N/A, and bind the profile identifier
  into the canonical deal context so two peers cannot start a hand under
  different rules.
- **Acceptance gate:** profile document exists with per-rule citations;
  profile identifier is bound into the canonical deal context and a
  mismatch fails closed; a control proves two peers with different
  profile identifiers refuse to start a hand together.
- **Non-goals:** engine conformance fixes (M7); changing any existing
  engine behaviour; adopting a TDA 2026 identifier before TDA publishes
  one.
- **Known limitations:** collusion, soft play and chip dumping are
  *policy*, not enforceable invariants, in a table with no operator. The
  profile must say so rather than imply protection it cannot deliver.

### M2 — Suspension, reconnect, and crash recovery

- **Status:** design in progress
- **Depends on:** M1 (profile binding) is helpful but not blocking
- **Goal:** define and prove exact-seat suspension and resumption. Cover
  the three failure classes separately: transport interruption with the
  process alive; process restart on the same device; device/secret loss.
- **Acceptance gate:** exact resume-state inventory; an authenticated
  exact-seat reconnect protocol that reuses the existing immutable
  seat↔signing-key binding; a transcript/recovery design with threat
  analysis covering omission, reordering, equivocation, stale/future-hand
  injection, seat and key substitution, reconnect under a new `conn_id`,
  a relaying peer lying about history, and loss of locally generated
  never-broadcast state; a SUSPENDED state machine with deterministic
  exit; controls proving committed chips are never refunded.
- **Non-goals:** threshold cryptography of any kind; changing admission
  beyond what the analysis shows is required; the production deadline
  ticker.
- **Known limitations:**
  - **B1** means a permanently lost participant leaves a multi-live-player
    hand **BLOCKED / UNRECOVERABLE**. That must not be mapped to
    automatic void-and-refund — frozen value is preferable to a
    profitable disconnect primitive.
  - The **sole-live-player settlement exception** (award the pot without
    board recovery when exactly one poker-live seat remains) has been
    examined and **REFUTED**. Two independent adversarial reviews
    rejected it with high confidence: the audit is the only point in the
    protocol where a seat publishes a proven share for its **own** hole
    cards, and the exception's safety depends on a prevention-mode gate
    that is transport-conditional (**D4**). Do not implement it. See
    `docs/research/p2-suspension-reconnect.md` for the conditions any
    revival would have to meet.

### M3 — Corrected timeout contract

- **Status:** blocked on M2
- **Depends on:** M2
- **Goal:** rewrite `docs/TIMEOUT_SPEC.md`. The current document is wrong
  in three places: it resolves the action-vs-proposal race using
  `action_seq` ordering (which diverges), promises a betting timeout at
  every seat count (n = 2 must not have one), and treats a
  deal-contribution timeout as void-with-stacks-preserved (which refunds
  the silent player).
- **Acceptance gate:** spec encodes the three conditions (acted /
  stalling-but-present / cryptographically absent); T, Δ, L bound
  canonically with G and E derived rather than transmitted; explicit
  n = 2 limitation; explicit statement that a victim's local clock
  reading is not independently verifiable evidence.
- **Non-goals:** implementation; the production ticker; a trusted poker
  server or any broad third-party authority.
- **Known limitations:** a residual fairness leak of `L + 2Δ` (15 s at
  the accepted parameters) during which a modified client may
  occasionally have a late action honoured. This is a fairness cost, not
  a safety one — convergence is unaffected.

### M4 — Timeout certificate applicability and convergence

- **Status:** blocked on M3. **Supersedes P2c `6b553a8`.**
- **Depends on:** M3
- **Goal:** implement certificate-based betting timeout for n ≥ 3 with
  frozen membership, honest-signing discipline (never sign after
  observing a valid action), and an outcome function containing no local
  clock comparison.
- **Acceptance gate:** controls A–L green, including deliberate breaks;
  all delivery permutations of the same evidence produce one outcome;
  stale/future sequence, wrong hand, wrong actor, wrong phase, wrong
  profile and wrong parameters all rejected.
- **Non-goals:** heads-up automatic timeout; eviction (unavailable — see
  B1); the production ticker.
- **Known limitations:** solves only the *stalling but present* peer. It
  does nothing for the failure users actually experience (Wi-Fi drop,
  app crash, laptop sleep), which is why it sits behind M2.

### M5 — Rebase and finish P2a phase ownership

- **Status:** parked at `6955f4f`, two tests red on purpose
- **Depends on:** M4
- **Goal:** correct timeout phase ownership so `deal_shuffle` /
  `deal_decrypt` apply only while awaiting cryptographic contributions,
  and betting semantics own the timeout once in `PHASE_BETTING`. Restore
  the ownership guard on `_maybe_start_deadline` (**B5**).
- **Acceptance gate:** the two parked red tests pass for the right
  reason; ownership guard restored and proven by a deliberate break.
- **Non-goals:** re-litigating the timeout contract settled in M3.

### M6 — P2b production scheduler

- **Status:** blocked on M5. PR #36 is closed/unmerged and forensic only.
- **Depends on:** M5
- **Goal:** make the deterministic deadline machinery reachable from the
  production sidecar lifecycle. One timeout system, not two.
- **Acceptance gate:** production path demonstrably arms and fires
  deadlines; no wall-clock waiting in tests; the mixed-queue ownership
  question that blocked PR #36 answered definitively.
- **Non-goals:** a second generic bus pump; exposing timeout scaling as a
  normal production CLI knob (test-only, e.g. `--test-deadline-scale`).

### M7 — Professional-rules engine conformance

- **Status:** ready to start; independent of the P2 crypto chain
- **Depends on:** M1
- **Goal:** close the conformance gaps found by the rules audit —
  cumulative short all-ins reopening betting (**B6**); reject rather than
  silently coerce a sub-minimum raise; consider an engine-level turn
  guard.
- **Acceptance gate:** each fix has a deliberate-break control; no
  existing behaviour regresses.
- **Non-goals:** rewriting the engine; adding physical-casino rule
  emulation.

### M8 — Remote Godot P2P

- **Status:** blocked
- **Depends on:** M2, M4, M6
- **Goal:** first genuinely remote multiplayer hand over real transport.
- **Acceptance gate:** a full hand completes between two machines, with
  suspension and reconnect exercised at least once.
- **Non-goals:** performance tuning.

### M9 — Physical desktop ↔ laptop acceptance

- **Status:** blocked
- **Depends on:** M8
- **Goal:** run on real hardware across a real network, using the same
  Δ = 5 s / L = 5 s parameters rather than a special easy LAN profile.
- **Acceptance gate:** repeated hands complete; observed skew and latency
  recorded to validate or correct Δ and L.
- **Known limitations:** the accepted Δ and L are conservative estimates,
  not measurements. This milestone is where they become measurements.

### M10 — Performance profiling

- **Status:** blocked
- **Depends on:** M9
- **Goal:** measure the real cryptographic hand-start cost end to end.
- **Acceptance gate:** a profile identifying the actual bottleneck.
- **Non-goals:** optimising anything before it is measured.

### M11 — Rust / MSM, only if profiling justifies it

- **Status:** conditional, blocked
- **Depends on:** M10
- **Goal:** targeted native acceleration.
- **Acceptance gate:** M10 shows a bottleneck that native code addresses.
- **Non-goals:** starting this work speculatively.

### M12 — Independent whole-protocol audit

- **Status:** blocked
- **Depends on:** M8 (at minimum)
- **Goal:** independent adversarial review of the complete protocol.
- **Acceptance gate:** findings triaged and either fixed or accepted with
  written rationale.

### M13 — RC / adversarial matrix

- **Status:** blocked
- **Depends on:** M12
- **Goal:** release-candidate adversarial test matrix.
- **Acceptance gate:** matrix green.

---

## Carried follow-ups

Not yet milestones; do not lose them.

| ID | Item |
|---|---|
| M9-gap | `hand_failed` predicate in the sidecar is documented as an open gap |
| N1 | (carried from PR #34 review) |
| L3, L8 | (carried) |
| LOW-4, LOW-5 | (carried) |
| — | Straddle behaviour unverified against TDA provisions |
| — | Detection-only chain attribution needs a new message exchange; currently `bad_seat` is `None` for a corrupt deck with no bad decryptor |
