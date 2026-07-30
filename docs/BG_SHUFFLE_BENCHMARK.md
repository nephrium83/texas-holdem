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

## Decision

The Bayer–Groth prevention proof is fast enough to continue toward an opt-in
prevention mode. It should not become the default yet. The next benchmark must
run the proof through the real `MentalDeal` shuffle chain and compare:

1. detection-only startup;
2. Bayer–Groth prevention startup; and
3. the existing cut-and-choose prevention path.

No timing result changes the security parameters. If the integrated path misses
the performance budget, optimize the measured bottleneck or retain detection
only as the default.
