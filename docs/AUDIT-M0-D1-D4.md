# M0 — security cleanup D1–D4

Scope: the four defects recorded against `main` in issue #37, and nothing
else. No reconnect, suspension, timeout certificates, transcript recovery,
profile binding, B7, or deadline scheduling. `6955f4f` (P2a) and `6b553a8`
(P2c) are untouched.

This file exists because the findings were living in a pull request
description and an issue comment. Those are the right place to *report*;
they are not a durable place to *keep*. Everything below is checkable
against the code and the tests in this branch.

## Verdicts

| Defect | Verdict | Where the rule now lives |
|---|---|---|
| **D1** partial seat-key freeze | real, fixed | `Session._bind_seat_keys` |
| **D2** mid-hand `player_info` | real, fixed | `Session._on_player_info` |
| **D3** `all_hole_cards` exposure | **not a defect at the named site** | no code change |
| **D4** BG enforcement keyed to `author_mode` | overstated; the real risk fixed | `MentalDealDriver.__init__`, `Session._assert_deal_preconditions` |

## D1 — the seat→key map is all or nothing

`_seat_keys` is authoritative *and* one-way, so the interesting property is
not only "an unresolved seat is never trusted" (it always was) but "the
session never reaches a state where an honest seat can never be authorized
again". Three ways the map can come out meaningless, all refused at the
freeze:

1. **Partial.** Some seats resolved, some did not. Reachable with no
   attacker: `handle_disconnect` pops a peer from `players`, so a drop
   between `start_game` and `start_p2p_hand` leaves its seat keyless.
   Refused, and the map is left unfrozen so a later complete attempt can
   still succeed.
2. **Empty in WIRE mode.** Authorization does fail closed afterwards — with
   no bindings, `_author_owns_seat` refuses every seat rather than trusting
   the delivering connection — but at the wrong *moment*: the hand starts
   and every message is then refused, a dead table rather than a clean
   refusal. Refused at bind time.
3. **A repeated seat id.** A seat id names one identity and one driver.
   `["a", "b", "a"]` freezes a complete-looking 3-of-3 map in which one key
   is the authority for seats 0 and 2, while the peer holding it reports
   `local_seat == 0` and drives one driver — so the n-of-n deal waits
   forever for shares from a seat nobody is playing. Refused at the freeze,
   and refused earlier at `_on_game_start`, which used to adopt whatever
   seat order a host sent. `configure_seats` has always enforced this for
   the local API; all three now go through one rule,
   `session._duplicate_seat_ids`.

**An EMPTY map stays legitimate in compat.** That transport carries no
envelopes, so no seat has a verified key at all and `_author_owns_seat`
falls through to the `conn_id` rule by design. Empty means "this transport
has no authors"; partial means "this transport has authors and we lost
some". An empty *seat order* is not an error in either mode: "no seats yet"
is not "seats that will not resolve".

### What D1 does not establish

That two different seats hold two different identities. A host that asserts
one signing key for two `conn_id`s produces a legal, complete map in which
one author speaks for two seats. That is the guarantee `admission.py`
already declines to offer — a joiner is not party to any other peer's
admission and holds no attestation for it — and the admission secret is
shared by everyone invited, so a holder can complete the handshake once per
identity it generates. Closing the same-key case at the freeze while the
roster it is built from remains the host's assertion would be theatre.
Stated here rather than silently assumed away.

### Liveness consequence, deliberately accepted

D1's refusal raises out of `start_p2p_hand`. A peer dropping in the freeze
window now *prevents the hand starting* rather than starting a hand with a
dead seat. That is fail-closed as specified, and it is still a liveness
change: it will interact with M2's suspension semantics and should be
revisited there, not reverted here.

## D2 — `player_info` is a LOBBY message

Roster identity is established in the lobby. Accepting `player_info` during
PLAYING let any holder of the room code land in `players` and `_join_order`
mid-hand and trigger a roster broadcast. It gains no seat — `_seat_order`
and `_seat_keys` are frozen before the first hand — but lobby state is not a
scratchpad, and `_on_player_ack` and `_on_game_start` already carried this
perimeter. The guard is keyed on terminality as well as state, because
`terminate()` sets state to `ENDED` and a check on PLAYING alone would lapse
exactly when the session dies.

## D3 — investigated, not fixed, and that is the finding

The recorded defect does not exist at the named site. `deck_audit.py:33`
documents the full-deck reveal as accepted by design: once the audit
completes every peer holds all 52 positions, so `all_hole_cards()` is
downstream convenience over data the peer already has, and a modified client
would compute the same thing from `audit_report.cards` and `hole_positions`.
Gating the method would be theatre.

The real question — whether the audit should expose *mucked* hole cards on a
fold-win, which TDA convention says are not shown — is a poker-rules
question for **M1**, not a code defect for M0. D3 is closed as *not a defect
at the named site*, **not** as *no exposure exists*. The exposure is real and
by design.

## D4 — the archaeology first, then the residual risk

Which paths are compat, and are they shipped or adversarial?

* `transport.py` declares `delivers_verified_envelopes = True` → **wire**,
  the production remote transport.
* `inmemory_transport.py` and `tcp_transport.py` declare `False` → compat.
* `sidecar_launcher.py` builds an `InMemoryBus`, so compat **is** a shipped
  path — but every seat lives in one process, so there is no remote hostile
  peer, and it hard-codes `deal_policy = bayer-groth-v1` with a comment
  explaining why.
* `SimpleTcpTransport` is constructed nowhere under `holdem/` — test-only.
* `_deal_first_hand`, which takes a `deal_policy` argument, is documented as
  not on the production `run()` path.

So compat is not a shipped hostile-peer path, and detection-only there is an
explicitly declared mode rather than a bypass. **What was genuinely wrong is
that the insecure state was reachable by omission**: `MentalDealDriver`
defaulted `prevention` to `False`, so any construction site that forgot the
argument would deal detection-only while the table believed otherwise. It is
now a required keyword with no default.

The mandate in `_assert_deal_preconditions` is additionally keyed to the
**adopted policy** first and `author_mode` second. That guard cannot fire
today — `prevention` is derived from the policy — and that is the point: it
pins a derivation the `prevention` docstring itself warns can drift. It is a
**pin, not a discovered bug**, and its control has to force the drift
artificially to prove the test depends on the guard.

## Evidence

Executable, on every CI job:

* `tests/test_m0_security.py` — the invariants, one test per property.
* `tests/test_m0_security_controls.py` — one deliberate-break control per
  guard, each naming the invariant test it is the control **for**. The
  pre-fix behaviour is staged with `monkeypatch` rather than by hand-editing
  `session.py`, so the controls re-run instead of being a paragraph saying
  someone once watched them fire. Where a guard is a shared rule rather than
  a rewritten method, the break is staged at the rule.
* Negative controls sit beside the breaks, so "refuses everything" cannot
  satisfy them: compat's empty map, lobby admission, a legitimate seat
  order, an empty seat order in wire mode, and a declared detection-only
  compat table all stay green against the real code.
* `tests/test_crypto_gate.py` and `tests/crypto_gate.py` — a run that
  reports success must either have exercised the crypto-gated suites or have
  said out loud that it did not. The status line is printed from
  `pytest_terminal_summary`, because CI runs `pytest -q` and the run header
  is suppressed at negative verbosity.

Reproductions on `origin/main` before the fixes, and the CI results for each
pushed head, are recorded in issue #37 and PR #39 rather than duplicated
here.

## Test platform

`engine-tests (3.10)` failed once on `46e7272` with `test_game_fuzz`,
`Timeout (>60.0s)`, and went green on a re-run of the identical commit. It
is not input flakiness — the test is fully seeded — it is a deterministic
~30 s test against a 60 s suite-wide budget, on the slowest interpreter on a
variable runner. It now carries its own `pytest.mark.timeout(300)`. That is
a budget change only: same seeds, same work, same invariants. A test whose
failure says nothing about what it asserts costs review attention every time
it flips, and this one was going to keep flipping.

## Known limitations

* The policy-keyed D4 guard is load-bearing only if someone later makes
  `prevention` settable independently of the policy.
* `SimpleTcpTransport`'s self-asserted cleartext `conn_id` handshake is
  untouched. Test-only, so out of M0 scope; it must not become a shipped
  path without work.
* A test that imports a crypto-backed module *inside its body* — because
  the import is the thing under test — must call
  `crypto_gate.require_crypto()` first, or it reports an ERROR rather than a
  skip on a machine without libsodium. Both current cases do. Nothing
  enforces the convention for a future one; what is enforced is the louder
  property, that a run cannot report success while silently omitting the
  crypto estate.
* `_on_game_start` still adopts a seat order without checking its *shape*
  (element types, seat count, local seat present). Non-`str` ids already
  fail closed later, where `_deal_context_bytes` refuses to encode them, and
  tightening the rest belongs with the seat-identity work in M2/B7 rather
  than here.
