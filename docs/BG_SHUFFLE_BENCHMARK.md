# Bayer–Groth shuffle benchmark

**Measurement correction, 2026-10-27:** the numbers below are historical
July 30 reports, not acceptance evidence for the current build. Their p95
used the former undercounting index; no corrected p95 is asserted without
recomputing the original samples. Current scripts use nearest-rank p95.
The coordinator benchmark excludes real transport. Its legacy "wire"
figures counted logical messages once, and its "cold" records were first
hands of configurations sharing one process. They do not establish network
bandwidth, cold-process performance or the complete product timing gate.

This is a proof-only benchmark for the standalone `bg_shuffle` prevention
path. It measures one 52-card shuffle round, including proof generation and
verification. It does not include key generation, mental-poker orchestration,
network transfer, serialization, or card dealing.

Run on BWING on 2026-07-30:

- Windows 11
- Python 3.13.3
- 10 samples for the recommended 4×13 layout
- 5 samples for the comparison layouts

| Matrix layout | Prove p50 | Verify p50 | Proof payload |
|---|---:|---:|---:|
| 2×26 | 115 ms | 35 ms | 3,520 bytes |
| **4×13** | **132 ms** | **29 ms** | **2,976 bytes** |
| 13×4 | 254 ms | 36 ms | 5,280 bytes |

The 4×13 layout is the current choice because it gives the smallest proof
among these factorizations and keeps proving substantially faster than the
13×4 layout.

## Serial chain estimates

Using the 4×13 p50 values, the rough cost of one proof per shuffler is:

| Seats | Prove + one verification per round | Historical estimate excluding shuffler verification |
|---:|---:|---:|
| 2 | 322 ms | 322 ms |
| 4 | 644 ms | 875 ms |
| 9 | 1,448 ms | 3,270 ms |

These estimates are useful for deciding whether prevention is plausible, but
they are not the product performance gate. The final gate must measure the
complete DKG → shuffle → deal startup over the actual transport, including
serialization and all peer verifications.

## Integrated hand-start benchmark

Superseding the estimates above for the two modes now wired into the
coordinator. `benchmarks/mental_deal_startup.py` drives a complete
peer-symmetric hand — distributed key ceremony, full shuffle chain, and the
selective deal down to recovered hole cards — with every seat simulated
in-process and every message delivered to every seat, so verification
reflects the real n-verifiers-per-round load.

Run on BWING on 2026-07-30: Windows 11, Python 3.13.3, libsodium 1.0.22,
AMD64 16 cores, 4×13 layout, 20 hands per configuration (1 initial + 19
subsequent hands in a shared process). Reported warm p50 unless stated.
Transport is excluded; the historical byte column is logical JSON message
bytes, without recipient multiplication, signatures, framing or topology.

| Seats | Detection p50 | Prevention p50 | Prevention p95 | Budget target | |
|---:|---:|---:|---:|---|:--|
| 2 | 24.9 ms | 452 ms | 456 ms | p95 under 5 s | historical only |
| 4 | 88.5 ms | 1,267 ms | 1,272 ms | p95 under 5 s | historical only |
| 9 | 623 ms | 5,075 ms | 5,095 ms | p95 under 10 s | historical only |

These historical coordinator timings are below the numerical targets, but
do not pass the real-transport performance gate. Nine-seat prevention was
reported at about 5.1 s; the roughly 91 s cut-and-choose value is projected,
not a measured integrated hand.

**These figures supersede an earlier measurement of 399 / 1,062 / 4,106 ms.**
That baseline predates the soundness fix. Verification then did not
recompute the multi-exponentiation statement from the input deck, which is
the step that binds output to input; adding it costs a 52-term multiscalar
over ciphertexts (~104 point operations) in both prove and verify, taking
per-verification cost from 28.2 ms to 38.8 ms. Orchestration is unchanged
(626 -> 634 ms at nine seats), so the whole difference is the cost of
actually checking the shuffle. The earlier numbers measured a verifier that
accepted forged decks and are not a target to regain.

### Where the time goes

| Seats | Mode | Proving | Verifying | Serialization | Orchestration | Logical messages | Proofs |
|---:|---|---:|---:|---:|---:|---:|---:|
| 9 | detection | — | — | — | 623 ms | 104 KB | — |
| 9 | prevention | 1,269 ms | 3,143 ms | 35 ms | 634 ms | 182 KB | 78 KB |

**Verification is the hot path, not proving or serialization.** A hand runs
one proof per shuffler but `seats²` verifications, so proving scales linearly
in seats while verification scales quadratically; at nine seats that is 9
proofs against 81 verifications. Serialization is 35 ms of a 5.1 s hand and
is not worth optimizing.

Orchestration time (key ceremony, shuffle mechanics, deal) is measured with
proof work subtracted, and is identical across modes to within noise — 22.4
vs 22.3 ms keygen, 175 vs 174 ms shuffle, 428 vs 427 ms deal at nine seats.
This run isolates proof work from orchestration. A single first observation
per configuration, with shared process caches, cannot establish the size
or absence of cold-process warmup cost.

If the budget later tightens, the measured target is batch verification of
the multi-exponentiation argument, not proof size or wire format.

## Cut-and-choose comparison

The third arm. `shuffle_proof.py` is not wired into the coordinator, so it
cannot be run through the integrated harness, and wiring an inferior
construction into the live protocol purely to benchmark it is the wrong
trade. `benchmarks/shuffle_proof_comparison.py` instead measures both proof
systems on the same 52-card shuffle and projects each to a full hand with
the seat-chain model `seats * prove + seats² * verify` — one proof per
shuffler, verified by every peer.

The historical comparison reported +6.6% proving and +22.8% verification
error against one Bayer–Groth dataset. That does not validate an error bound
or show that another proof system has the same bias. These projections are
estimates; the script now labels the model `not_validated` explicitly.

Per 52-card round, 5 samples, p50, k = 128:

| | Prove | Verify | Proof size |
|---|---:|---:|---:|
| Bayer–Groth 4×13 | 138 ms | 35 ms | 3 KB |
| Cut-and-choose k=128 | 1,023 ms | 1,010 ms | 650 KB |
| Ratio | 7.4× | 29.2× | 217× |

Projected proof work per hand:

| Seats | Bayer–Groth | Cut-and-choose | |
|---:|---:|---:|---:|
| 2 | 0.41 s | 6.09 s | 15× |
| 4 | 1.10 s | 20.25 s | 18× |
| 9 | 4.04 s | 91.02 s | 23× |

These samples informed the choice of Bayer–Groth. They do not establish
full-transport performance or a bound for another construction. The
650 KB per-round payload estimate is 5.9 MB per nine-seat hand against
78 KB. The
roughly 20 s nine-seat figure previously carried for cut-and-choose
understates it by about 4.5×; 20 s is closer to its **four**-seat cost. That
figure predates this harness and appears to have counted a single verifier
rather than every peer.

`k = 128` is a full security parameter for the cut-and-choose path, not a
statistical one (see the module docstring). It was not reduced to flatter
the comparison, and the benchmark refuses a lower value.

## Decision

Bayer–Groth is the prevention path. It is wired into `MentalDeal` and
established table-wide by the host via `deal_policy`. Coordinator-only
measurements do not close the real-transport timing gate.

`shuffle_proof.py` stays in the tree as an unwired reference implementation.
It is a different construction resting on different assumptions (no
commitment key, so the trapdoor class is structurally impossible), which
makes it worth keeping as a cross-check, but nothing should ship on it.

Production peer-to-peer tables require the explicit `bayer-groth-v1`
policy. There is no implicit or downgrade default. Explicit detection-only
mode is retained for in-process harnesses and comparative benchmarks.

No timing result changes the security parameters.
