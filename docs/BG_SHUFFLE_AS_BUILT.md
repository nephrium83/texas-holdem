# Bayer–Groth shuffle prevention — as built

What shipped, what was wrong with it, and what the tests actually
establish. This is not the implementation plan. A plan describes an
intention; this describes the code in the tree, including the ways it
was wrong before it was right, because that is the part a later reader
cannot reconstruct.

Paper references are to the 30-page `MinimalShuffle.pdf`
(`www0.cs.ucl.ac.uk/staff/J.Groth/MinimalShuffle.pdf`). Section and
theorem numbers differ in the EUROCRYPT proceedings version.

Status: implemented, integrated opt-in, benchmarked. Detection-only
remains the `MentalDeal` default and the default wire format is
byte-identical to the pre-prevention protocol.

---

## 1. Module map

The plan anticipated a module named `bg_multiexp.py` carrying §4. That is
not what shipped. §4 lives in `bg_shuffle.py` as an internal
sub-argument, because the multi-exponentiation argument is not
independently useful here — nothing but the shuffle instantiates it — and
a separate module would have implied a public API that does not exist.

| Module | Paper | Domain tag | Lines |
|---|---|---|---:|
| `pedersen.py` | — | `poker.bg.pedersen.gen.v1` | 115 |
| `bg_svp.py` | §5.3 single-value product | `poker.bg.svp.v1` | 208 |
| `bg_zero.py` | §5.2 zero argument | `poker.bg.zero.v1` | 441 |
| `bg_hadamard.py` | §5.1 Hadamard product | `poker.bg.hadamard.v1` | 359 |
| `bg_product.py` | §5 product argument | `poker.bg.product.v1` | 164 |
| `bg_shuffle.py` | §4 multi-exp + §3 shuffle | `poker.bg.shuffle.v1`, `poker.bg.shuffle.multi-exponentiation.v1` | 489 |
| `bg_challenge.py` | — | — | 91 |
| `bg_witness.py` | — | — | 33 |

`bg_challenge.py` and `bg_witness.py` are infrastructure, not protocol.
The first is the single nonzero-challenge derivation rule; the second is
the prover-side witness self-check seam.

Tests: 220 across the nine BG files.

| File | Tests |
|---|---:|
| `test_bg_zero.py` | 62 |
| `test_bg_hadamard.py` | 38 |
| `test_bg_wire.py` | 22 |
| `test_bg_svp.py` | 21 |
| `test_bg_challenge.py` | 20 |
| `test_bg_soundness.py` | 17 |
| `test_bg_product.py` | 15 |
| `test_bg_shuffle_soundness.py` | 14 |
| `test_bg_shuffle.py` | 11 |

---

## 2. The unsoundness

`eaf21f7` fixed a break that made prevention mode security theatre: a
cheating shuffler could replace the whole deck with 52 copies of one
card, or substitute a completely different deck, and every peer accepted
it. `af9fb09` had already integrated that mode, so for the span between
those two commits the protocol spent roughly 4 s per nine-seat hand
buying nothing.

### 2.1 It was not the predicted channel

The pre-implementation audit predicted the primary soundness risk would
be the **main-diagonal forgery** — break #1 in the audit table — where a
prover encrypts a nonzero plaintext into `E_m` and the verifier fails to
require `c_B[m] == IDENTITY`.

**That check was implemented correctly and was never the problem.** It
is present in `_multi_verify` and always was:

```python
if proof.commit_b_k[m] != zero_commit or proof.vector_e_k[m] != product:
    return False
```

The audit was right that extraction passes through exactly two point
equalities, and right about which two. It picked the wrong one to worry
about. The defect was in the **sibling** equality — `E[m] == C_target`,
**break #4** in the same table, listed there as "E[m] == C_target
dropped."

The general lesson is not "the audit was wrong." It is that the audit
correctly identified the pair of load-bearing checks and then
concentrated its attention on the one whose failure mode was easier to
narrate. The one that broke was the one requiring the verifier to
*compute* something rather than to *compare* something.

### 2.2 The two defects

**Defect 1 — the verifier never recomputed the statement.** `verify()`
passed `product=proof.multi.vector_e_k[m]` into `_multi_verify`, which
then compared that value against itself. The right-hand disjunct above
was `x != x`, permanently false. Paper step 6 requires the verifier to
compute `prod_j C_j^{x^j}` from the public input deck; that product is
the only thing binding `out_deck` to `in_deck`. Without it, `in_deck`
entered verification solely through the Fiat–Shamir statement hash —
which stops post-hoc tampering with a finished proof, and does nothing
against a prover forging from the start.

**Defect 2 — the multi-exponentiation ran over the wrong exponents.**
`_multi_prove` received the permutation index matrix where the argument
requires the `b` matrix of `x^perm` powers, and `_multi_verify` checked
the opening against `a_commits` rather than `b_commits`. `b_chunks` was
passed in and never used.

The two were mutually consistent, which is why the honest path verified
and nothing looked wrong: the prover fed `vector_e_k[m]` into the same
challenge hash the verifier used.

### 2.3 Why the original tests missed it

All 11 `bg_shuffle` tests passed against the vulnerable code. They
covered completeness (honest proofs verify) and tamper-resistance
(mutating a finished proof breaks it). Neither models the actual threat:
a prover that runs the honest algorithm over a deck it never shuffled.
`test_wrong_witness_rejected_by_prover` checked that the *prover*
rejects a bad witness — but soundness cannot rest on the prover policing
itself, since an attacker deletes that check first.

`_prove_unchecked` exists as a seam for exactly this: the real prover
minus its self-check, so the test suite can be that attacker. It is not
part of the public API.

---

## 3. Evidence

### 3.1 The regression is verified against the pre-fix code

Checking out `eaf21f7^` and running the soundness suite directly does
**not** work, and a report that it "fails" there is worthless: the tests
raise `AttributeError` at import, because `_prove_unchecked` and
`bg_witness` were introduced by the fix commits themselves. Failing on a
missing attribute demonstrates nothing about detection.

The sound experiment is to backport **only the test seams** — the
`_prove_unchecked` split and `bg_witness` — leaving both logic defects
intact. Under that configuration:

**8 tests fail with genuine assertion failures**, `assert True is False`:
the pre-fix verifier *accepted* the identical-card deck, the foreign
deck, the single substituted card, the duplicated card, the reordered
output, the unencrypted deck, and forgeries at every supported layout.
`test_honest_shuffle_still_verifies` and
`test_multi_exponentiation_uses_the_x_power_commitments` pass, as they
should — the honest path was never broken.

That is the verification. The fix is real and the tests detect its
absence.

### 3.2 The §5 sub-argument tests are coverage, not regression

`8dbf188` added `tests/test_bg_soundness.py` — 17 forgery tests across
`bg_zero`, `bg_hadamard`, `bg_product`, and `bg_svp`. These **cannot** be
verified against `eaf21f7^` and should not be described as regression
tests for this vulnerability. `eaf21f7` states explicitly that those four
modules are untouched, and they were: the bug was entirely in the link
from the committed vectors to the ciphertexts. Running that file against
the pre-fix tree fails on missing `_prove_unchecked` seams in the
sub-argument modules, which is an artifact of when the seams landed, not
evidence of anything.

They are new coverage for code that was never broken. Valuable; not
proof of this fix.

### 3.3 Reintroducing each defect — and a trap in doing so

`eaf21f7` claims that reintroducing either defect alone is now caught by
the other's fix. That claim was tested and **holds**, but only under a
faithful break, and the unfaithful version is easy to reach by accident.

**Defect 1 cannot be reintroduced on the verifier alone.** `product`
also feeds `_multi_challenge`. Changing only `verify()` desynchronizes
the prover's and verifier's challenges, and the proof is rejected for a
reason unrelated to the defect. The suite goes green — 41 passed,
nothing fires — which invites the conclusion that defect 1 is
unreachable while defect 2 is fixed. **That conclusion is wrong. The
break was.** A rejection that looks like detection is worse than no
control at all.

Faithful defect 1 is two-sided, as the pre-fix code was: the prover
hashes `vector_e_k[m]` into the challenge, the verifier reads the target
back out of the proof.

| Break | Result |
|---|---|
| Defect 1, verifier only (**unfaithful**) | 41 passed — nothing fires; rejection comes from a challenge mismatch |
| Defect 1, two-sided, defect 2 fixed | **8 fail** — all six forged decks, the layout sweep, and the boundary test |
| Defect 2, both sides, defect 1 fixed | **7 fail** — completeness dies: honest shuffles stop verifying |
| Both defects | the original vulnerability |

Defect 2 alone is not a coherent state to sit in. Running the
multi-exponentiation over the index matrix breaks the honest relation, so
`test_honest_shuffle_still_verifies` and the three completeness tests in
`test_bg_shuffle.py` fail immediately.

### 3.4 A boundary that had no coverage

`test_verifier_recomputes_the_product_from_the_input_deck` is named for
defect 1 but calls `_multi_verify` directly, handing it a correctly
derived `expected`. It pins the sub-function. The defect lived one level
up, in `verify()`, which sourced that argument from the proof —
so reintroducing it changes nothing that test can observe.

`test_verify_recomputes_target_from_statement_not_proof` (`a19f285`)
closes that. It runs the honest algorithm over a deck that is not a
shuffle of the input, producing a proof whose every batched equation
balances and whose `E_m` is the multi-exponentiation of the *forged*
deck. The only thing separating it from a valid proof is
`E_m == sum_j x^{j+1} in_deck[j]`, and only a verifier deriving the
right-hand side from the statement can check it. It asserts
`E_m != target` explicitly so it cannot pass vacuously, then calls the
public `verify()`.

The accurate summary of what the suite establishes:

> The combined regression suite detects the original two-defect
> vulnerability. Under a faithful two-sided break, defect 1 alone is
> detected by the forged-deck set; defect 2 alone is caught immediately
> by completeness. A dedicated public-path regression now pins defect 1
> at the `verify()` call site, which previously had no coverage.

---

## 4. Fiat–Shamir

Every challenge in the stack comes from `bg_challenge.nonzero_challenge`
(`7010f94`): append a fixed-width counter to the transcript and advance
until the reduction is nonzero. This replaced six sites across four
modules that raised on a zero digest, in three different spellings.

Rejecting is not a derivation rule. Under Fiat–Shamir the transcript is
fixed by the statement and the initial message, so it is not the
prover's to choose; a prover whose transcript hashes to zero has nothing
to resample. The event is unreachable at ~2⁻²⁵², but the semantics were
wrong and the modules had drifted.

Injectivity comes from the fixed-width **trailing** counter: two
`(label, counter)` pairs give the same bytes only if the counters match,
and hence only if the labels do. The label length prefix is **defensive,
not load-bearing** — it survives a future change to the counter width,
which the injectivity argument would not. This is stated precisely
because it is demonstrable: no encoding break, including dropping the
prefix entirely, makes the two collision tests in
`test_bg_challenge.py` fire. Those two are consistency checks, not
verified-failing tests, and are labelled as such.

Where one transcript yields several challenges — `bg_hadamard`'s
`(x, y)` and `bg_shuffle`'s `(y, z)` — they are drawn under distinct
labels with **independent counter sequences**, so a retry on one cannot
shift the other. Callers must not derive one digest and split it.

### 4.1 Transcript binding

The rule the whole stack follows: **every field the verifier checks is
inside the hash**, and every variable-length field is length-prefixed.
The reason is the 2019 Swiss Post break — and it is not theoretical
here. During the §5.2 work, dropping the bilinear map from the hash left
all 53 tests green, because the map also participates in the arithmetic
and its absence was masked by an arithmetic failure.

**End-to-end tests cannot detect a missing transcript field when that
field also participates in the arithmetic.** Every transcript field
therefore gets a direct assertion on the challenge function, not just an
end-to-end rejection test. `test_transcript_binds_c_B` is the canonical
case: omitting `c_B` breaks Theorem 9's extraction while leaving the
other 37 Hadamard tests passing.

Commitment-key binding is hashed **generator by generator**, not as a
seed reference. The test substitutes one generator with `H` and the seed
untouched, which is the actual trapdoor shape; a whole-key swap does not
isolate it.

---

## 5. Measurements

Standalone proof-only, BWING, 2026-07-30, Python 3.13.3, 4×13 layout:
prove 132 ms p50, verify 29 ms p50, proof 2,976 bytes. The 13×4 layout
is roughly twice as slow to prove and 1.8× larger; 2×26 proves slightly
faster but produces a larger proof. **Small `m` wins**, as predicted.

Integrated hand-start — full DKG, shuffle chain, and selective deal,
every seat in-process, every message delivered to every seat:

| Seats | Detection p50 | Prevention p50 | Prevention p95 | Budget |
|---:|---:|---:|---:|---|
| 2 | 24.9 ms | 452 ms | 456 ms | p95 < 5 s — pass |
| 4 | 88.5 ms | 1,267 ms | 1,272 ms | p95 < 5 s — pass |
| 9 | 623 ms | 5,075 ms | 5,095 ms | p95 < 10 s — pass |

Figures in `docs/BG_SHUFFLE_BENCHMARK.md` were re-measured after the
soundness fix (`a2d7599`, `3e1a7a3`); the fix added a real
multi-exponentiation to every verification, so pre-fix numbers are not
comparable and must not be cited.

For context, the cut-and-choose shadow-deck proof it replaces measured
**19.02 s** at nine seats. Prevention at nine seats is roughly **3.7×
cheaper** than that, on the integrated path, measured rather than
estimated.

§4.1 (FFT) and §4.2 (multi-round folding) remain out of scope. The
paper's Table 2 settles it: FFT is *slower* than plain
multi-exponentiation at m=8 and m=16 and "only kicks in for m > 16." At
m=4 there is nothing to fold, and extra rounds mean more transcript
surface under Fiat–Shamir. Revisit only if a layout with m ≥ 16 or a
deck beyond ~10⁴ cards is ever selected.

---

## 6. Standing constraints

- Detection-only stays the `MentalDeal` default. `prevention=True` is
  opt-in and the default wire format is byte-identical to the
  pre-prevention protocol.
- The commitment key is transparent NUMS. Never accept a
  prover-supplied commitment key.
- `bg_zero` keeps `m >= 1`, `n >= 1`. Stricter bounds belong in callers
  — `bg_hadamard` requires `m >= 2` because the statement degenerates at
  m=1, and that bound lives in the adapter.
- `bg_zero`'s asymmetric B-fold is exact: `sum_{j=0}^{m} x^{m-j} c_Bj`,
  one sum, no separate `c_Bm` addend.
- `_prove_unchecked` is a test seam. Do not call it outside tests.
- No language change is indicated. The bottlenecks are per-call ctypes
  overhead on `crypto_scalarmult_ristretto255`, `multiscalar_mul`
  batching, and scalar fold loops — all addressable in Python if they
  ever need to be.

---

## 7. Conventions these commits were held to

Recorded because they caught real defects here, not as general advice.

**Every test must be verified to fail against a deliberately broken
implementation before it lands, and the control must name which test
fires.** Two §5.2 tests and two `bg_challenge` tests were passing for
the wrong reason until this was run.

**A break must be faithful.** §3.3 above is the cautionary case: an
incomplete break produced a green suite and an inverted conclusion. When
a control shows nothing firing, suspect the break before the tests.

**Commit before running controls.** A `git checkout -- tests/...` used
to revert a control silently wiped uncommitted test additions, and the
suite went quietly back to a lower count.

**CI is part of the evidence.** These 33 commits were green locally on
Windows / Python 3.13 for weeks and had never been compiled against
another version. The first CI run failed on 3.10 — a task-registry test
asserting a count delta, where a STUN task that dies instantly on 3.10
(`loop.sock_sendto` arrived in 3.11) masked the increment. The transport
was correct; the test was not.
