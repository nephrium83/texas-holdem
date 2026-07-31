# Cryptographic performance budget

This document defines the initial performance contract for serverless, cryptographically verifiable poker. It complements the detailed architecture and measured baseline in [L5_SCOPE.md](L5_SCOPE.md).

## Product policy

- **Detection-only** is the v1 default: the hand is audited and cheats are detected without making every shuffle carry a prevention proof.
- **Prevention** is available as an explicit opt-in, set table-wide by the host via `bg_prevention` in the table settings. Proof generation and verification are integrated into `MentalDeal` and the complete path is benchmarked below.
- No mode may reveal cards or accept a deal that failed its required verification.
- Security parameters are not reduced to meet a timing target.
- Prevention is not the default. Peers do not negotiate the mode, so a host that omits the flag downgrades the whole table to detection-only; a peer that will not accept that constructs its `Session` with `require_prevention=True` and refuses to be dealt in.

## Initial targets

These are engineering targets for the next integrated proof path, not claims about the current implementation.

| Operation | Target |
| --- | --- |
| Normal betting action, LAN p95 | under 500 ms |
| Normal betting action, internet p95 | under 1.5 s |
| Detection-only hand startup at nine seats | under 1 s |
| Prevention hand startup at 2–4 seats | p95 under 5 s |
| Prevention hand startup at nine seats | p95 under 10 s |
| Typical hand startup above 20 s | performance failure requiring investigation |

The Bayer–Groth prevention path is now integrated into `MentalDeal` behind a default-off flag and has been measured end to end. Against the targets above:

| Seats | Detection p50 | Prevention p50 | Prevention p95 | Target | |
| ---: | ---: | ---: | ---: | --- | :-- |
| 2 | 24.9 ms | 399 ms | 409 ms | p95 under 5 s | pass |
| 4 | 88.5 ms | 1,062 ms | 1,071 ms | p95 under 5 s | pass |
| 9 | 623 ms | 4,106 ms | 4,117 ms | p95 under 10 s | pass |

Method, per-phase breakdown, and byte counts are in
[`BG_SHUFFLE_BENCHMARK.md`](BG_SHUFFLE_BENCHMARK.md); the harness is
`benchmarks/mental_deal_startup.py`. Detection-only at nine seats measures
0.62 s here rather than the 0.18 s L5 figure, because this harness runs every
seat in one process and counts all peers' work, not one peer's.

The remaining unmeasured arm is the cut-and-choose path at roughly 20 seconds
for nine seats. That figure is an L5 estimate: `shuffle_proof.py` is not wired
into the coordinator, so it cannot be run through this harness for a
like-for-like comparison.

Per the optimization order below, the measured dominant phase is **verification**
(2,284 ms of a 4,106 ms nine-seat hand), not proving (1,162 ms) or serialization
(35 ms). Verification scales quadratically in seats — one proof per shuffler but
one verification per shuffler per peer — so batch verification of the
multi-exponentiation argument is the first optimization to reach for if these
targets tighten.

## Benchmark protocol

The benchmark issue is [Benchmark and optimize the complete cryptographic hand-start path](https://github.com/nephrium83/texas-holdem/issues/16).

Each benchmark run should record:

- 2-, 4-, and 9-seat configurations;
- at least 20 hands per configuration;
- cold and warm runs;
- shuffle generation time;
- proof-generation time per peer;
- proof-verification time per peer;
- bytes generated and transferred;
- total hand-start delay;
- p50, p95, and maximum;
- CPU, memory, Python version, libsodium version, and commit SHA.

Run the benchmark on the target hardware as well as CI hardware. CI should publish results as artifacts before absolute timing thresholds become merge gates.

## Optimization order

1. Measure the complete path.
2. Identify whether proving, verifying, serialization, transport, or orchestration dominates.
3. Optimize the dominant phase without changing the security contract.
4. Re-run the same benchmark matrix.
5. Consider native or batched implementations only when the measured hot path justifies them.
6. Revisit the default prevention policy only after the results meet the targets.

A benchmark result is not complete until it includes both a timing result and a statement that the proof and verification behavior is unchanged.
