# Cryptographic performance budget

This document defines the initial performance contract for serverless, cryptographically verifiable poker. It complements the detailed architecture and measured baseline in [L5_SCOPE.md](L5_SCOPE.md).

## Product policy

- **Detection-only** is the v1 default: the hand is audited and cheats are detected without making every shuffle carry a prevention proof.
- **Prevention** is planned as an explicit opt-in after proof generation and verification are integrated and the complete path has been benchmarked.
- No mode may reveal cards or accept a deal that failed its required verification.
- Security parameters are not reduced to meet a timing target.

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

The existing L5 baseline is the reference point until a new benchmark supersedes it: detection-only is about 0.18 seconds at nine seats, while the current prevention path is about 20 seconds at nine seats.

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
