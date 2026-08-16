# Cryptographic performance budget

This document defines the initial performance contract for serverless, cryptographically verifiable poker. It complements the detailed architecture and measured baseline in [L5_SCOPE.md](L5_SCOPE.md).

## Product policy

- **Detection-only** is the v1 default: the hand is audited and cheats are detected without making every shuffle carry a prevention proof.
- **Prevention** is MANDATORY for peer-to-peer play, set table-wide by the host via `deal_policy: "bayer-groth-v1"`. Proof generation and verification are integrated into `MentalDeal` and the complete path is benchmarked below.
- No mode may reveal cards or accept a deal that failed its required verification.
- Security parameters are not reduced to meet a timing target.
- There is no default. A table that declares no `deal_policy` is refused before it starts, and on a verified-envelope transport `"bayer-groth-v1"` is the only admissible value — so a host cannot downgrade a table by omitting a flag, which is exactly how every shipped game came to run detection-only. `"detection-only-v1"` remains legal for in-process harnesses and benchmarks, stated explicitly. The former per-peer `require_prevention` opt-out is gone: it was a second, overlapping policy authority.

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
| 2 | 24.9 ms | 452 ms | 456 ms | p95 under 5 s | pass |
| 4 | 88.5 ms | 1,267 ms | 1,272 ms | p95 under 5 s | pass |
| 9 | 623 ms | 5,075 ms | 5,095 ms | p95 under 10 s | pass |

These supersede an earlier 399 / 1,062 / 4,106 ms measurement taken before the
shuffle-argument soundness fix; see [`BG_SHUFFLE_BENCHMARK.md`](BG_SHUFFLE_BENCHMARK.md).

Method, per-phase breakdown, and byte counts are in
[`BG_SHUFFLE_BENCHMARK.md`](BG_SHUFFLE_BENCHMARK.md); the harness is
`benchmarks/mental_deal_startup.py`. Detection-only at nine seats measures
0.62 s here rather than the 0.18 s L5 figure, because this harness runs every
seat in one process and counts all peers' work, not one peer's.

The cut-and-choose path is now measured too, on a per-round basis and projected
with a seat-chain model validated against the Bayer–Groth figures above: about
91 seconds of proof work at nine seats, and 650 KB per proof against 3 KB. The
roughly 20 second figure previously carried for it understates the cost by
about 4.5x — 20 seconds is closer to its four-seat cost. Details in
[`BG_SHUFFLE_BENCHMARK.md`](BG_SHUFFLE_BENCHMARK.md); harness is
`benchmarks/shuffle_proof_comparison.py`. Bayer–Groth supersedes it on every
axis, and `shuffle_proof.py` remains in the tree only as an unwired reference
implementation.

Event-loop responsiveness is measured separately by
`benchmarks/event_loop_latency.py`. Message handling runs on a dispatch worker
rather than the event loop, because inline verification blocked the loop for up
to 2.9 s across a nine-seat hand and made timeouts fire spuriously.

Per the optimization order below, the measured dominant phase is **verification**
(3,143 ms of a 5,075 ms nine-seat hand), not proving (1,269 ms) or serialization
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
