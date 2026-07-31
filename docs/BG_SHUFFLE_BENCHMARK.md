# Bayer–Groth shuffle benchmark

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

| Seats | Prove + one verification per round | Prove + every other peer verifies |
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
AMD64 16 cores, 4×13 layout, 20 hands per configuration (1 cold + 19 warm).
Warm p50 unless stated. Transport is excluded; byte counts are what a real
transport would carry.

| Seats | Detection p50 | Prevention p50 | Prevention p95 | Added | Budget target | |
|---:|---:|---:|---:|---:|---|:--|
| 2 | 24.9 ms | 399 ms | 409 ms | +375 ms | p95 under 5 s | pass |
| 4 | 88.5 ms | 1,062 ms | 1,071 ms | +974 ms | p95 under 5 s | pass |
| 9 | 623 ms | 4,106 ms | 4,117 ms | +3,483 ms | p95 under 10 s | pass |

All three targets pass with substantial headroom. Nine-seat prevention lands
at about 4.1 s against the roughly 20 s cut-and-choose L5 baseline.

### Where the time goes

| Seats | Mode | Proving | Verifying | Serialization | Orchestration | Wire | Proofs |
|---:|---|---:|---:|---:|---:|---:|---:|
| 9 | detection | — | — | — | 623 ms | 104 KB | — |
| 9 | prevention | 1,162 ms | 2,284 ms | 35 ms | 626 ms | 182 KB | 78 KB |

**Verification is the hot path, not proving or serialization.** A hand runs
one proof per shuffler but `seats²` verifications, so proving scales linearly
in seats while verification scales quadratically; at nine seats that is 9
proofs against 81 verifications. Serialization is 35 ms of a 4.1 s hand and
is not worth optimizing.

Orchestration time (key ceremony, shuffle mechanics, deal) is measured with
proof work subtracted, and is identical across modes to within noise — 22.4
vs 22.3 ms keygen, 175 vs 174 ms shuffle, 428 vs 427 ms deal at nine seats.
Prevention adds proof work and nothing else. Cold and warm runs also agree to
within noise, so there is no meaningful warmup cost to amortize.

If the budget later tightens, the measured target is batch verification of
the multi-exponentiation argument, not proof size or wire format.

## Decision

The Bayer–Groth prevention proof is fast enough to be offered as an opt-in
prevention mode, and it is now wired into `MentalDeal` behind a default-off
flag. It should still not become the default: the session layer does not yet
enforce a uniform table-wide mode, which is the remaining gate.

The comparison this document previously asked for is two-thirds complete.
Detection-only and Bayer–Groth prevention are measured above on the same
harness. The third arm — the existing cut-and-choose path — cannot be
measured end to end because `shuffle_proof.py` was never wired into the
coordinator, and wiring it was explicitly out of scope for the integration
task. The roughly 20 s nine-seat figure in
[`PERFORMANCE_BUDGET.md`](PERFORMANCE_BUDGET.md) remains the reference for
that path and is an L5 estimate, not a measurement from this harness.

No timing result changes the security parameters. If the integrated path
misses the performance budget, optimize the measured bottleneck or retain
detection only as the default.
