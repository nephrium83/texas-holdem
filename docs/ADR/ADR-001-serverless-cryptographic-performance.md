# ADR-001: Serverless cryptographic performance

- **Status:** Accepted
- **Date:** 2026-07-29
- **Scope:** Mental-poker deal, shuffle proofs, and player-facing hand startup

## Context

The project is intended to become serverless, cryptographically verifiable poker. Every peer must be able to verify the encrypted deal without trusting a dealer or host.

The cryptographic path is more expensive than ordinary poker actions. The current L5 measurements record detection-only audit at roughly 0.18 seconds for a nine-seat table and the opt-in prevention path at roughly 20 seconds per nine-seat hand. The detailed baseline and existing v1 policy remain documented in [L5_SCOPE.md](../L5_SCOPE.md).

## Decision

1. Cryptographic correctness remains a product requirement, not an optional diagnostic feature.
2. A card must not be revealed or accepted as valid until the applicable proof or audit rule has passed.
3. Detection-first remains the v1 default while the full prevention path is integrated, benchmarked, and optimized. Prevention is planned as an explicit opt-in after that path is available.
4. The complete hand-start path will be measured at 2, 4, and 9 seats before prevention is considered for a default user-facing mode.
5. Performance work must preserve the selected security parameters and transcript bindings. Reducing a security parameter to meet a timing target requires a separate security review.
6. The UI and protocol may perform proof work asynchronously and show progress, but they may not silently fall back to an unverified or weaker deal.
7. The performance gate measures the full path: shuffle generation, proof generation, proof verification, network transfer, and total hand-start delay.

## Consequences

- The proof stack needs a benchmark milestone after end-to-end integration.
- A slow proof path is an optimization problem or a product-mode decision, not permission to bypass verification.
- Normal betting actions should remain responsive; expensive work belongs at hand startup or card-deal boundaries.
- Any future change from detection-first to prevention-by-default must cite measured results on the target hardware.
