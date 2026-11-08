# Current delivery

Source status updated November 7, 2026. Packaged-baseline evidence below
was last verified October 27. This is a delivery and coordination record;
it does not change rules, protocol contracts, or authorize another feature.

## Current source and audit delivery

[PR #135](https://github.com/nephrium83/texas-holdem/pull/135) merged at
`b45f12b14b7b55892301e96617006ef544ff66b5` after all ten required CI checks
passed. The Codex audit allocations have no implementation-open findings:
the first allocation has 70 resolved entries and one not reproducible entry;
the second has all 46 entries resolved. These allocation counts do not mean
the entire historical audit or Claude's remaining work is complete.

The final delivery includes negotiated legacy-GUI rules, conserved solo
practice-wallet transfers, truthful aggregate cashout receipts, stronger
admission/routing assertions, and standalone test-runner failure propagation.
259 focused integration tests passed; a real legacy-GUI smoke run completed
15 hands. These source checks do not update or certify the installed preview.

AI Pair is parked. Codex and Claude are working directly under file ownership.
Claude's open PRs and remaining protocol/test follow-through remain separate
from the delivered Codex allocations. For audit #194, the owner selected
automatic recovery under the agreed casual-table rules, or a clear session
end when recovery is impossible. That is a product decision, not a claim
that its timeout implementation has shipped.

The existing embedding contract is a Python API for a trusted host; it is
not yet a native Godot, Unity, Unreal, C++ or C# SDK. See
[EMBEDDING.md](EMBEDDING.md) for host responsibilities and limits, and
[PACKAGING.md](PACKAGING.md) for the isolated installed-wheel acceptance.
At the source revision above, 149 embedding, package and core-boundary
checks passed. The wheel was installed without dependencies in a fresh
virtual environment outside the checkout. Its copied tavern example
completed two tables, checked private views and duplicate-payout handling,
and retained total funds of 8000. This verifies the existing Python-host
path; it does not establish another engine's native integration.

## Delivered baseline

The last verified packaged baseline is
`32a9966175d604ff3f5fe004dcd76070193f7d62`, the merge of
[PR #74](https://github.com/nephrium83/texas-holdem/pull/74). Later source fixes
do not automatically update the installed preview. Recheck main before
starting work; this SHA identifies the packaged baseline recorded here.

| Delivered work | Evidence |
| --- | --- |
| Explicit injected signing identities | [PR #59](https://github.com/nephrium83/texas-holdem/pull/59) |
| Explicit session resources and clocks | [PR #60](https://github.com/nephrium83/texas-holdem/pull/60) |
| Explicit native cryptography startup | [PR #62](https://github.com/nephrium83/texas-holdem/pull/62) |
| Casual LAN clocks and guest departure handling | [PR #66](https://github.com/nephrium83/texas-holdem/pull/66) |
| Public embedding API and separate host integration | [PR #70](https://github.com/nephrium83/texas-holdem/pull/70) |
| Authenticated Internet invitations and desktop Host/Join | [PR #74](https://github.com/nephrium83/texas-holdem/pull/74) |

The reusable poker package has no base runtime dependencies. Desktop,
networking, signing identities and currency resources are supplied through
host adapters and optional dependencies. Integrations should consume the
embedding API rather than copy the poker rules into another game.

The delivered casual profile uses direct UDP gameplay after authenticated,
encrypted connection setup. The desktop's trusted introduction service is
`wss://poker.errantsaints.space/v1`. That service handles setup; established
gameplay does not depend on it. The measured packaged acceptance route was
home-to-cloud. It does not establish universal NAT compatibility or acceptance
between two ordinary residential networks. See [DIRECT_INTERNET.md](DIRECT_INTERNET.md)
and [BUILD_WINDOWS.md](BUILD_WINDOWS.md) for the delivered route and gates.

## Contract and status precedence

The active [casual P2P rules](CASUAL_P2P_RULES.md) and
[poker rules profile](POKER_RULES_PROFILE.md) remain authoritative for their
respective behavior. Standard shared-pot ante allocation and the approved
casual departure rules remain in force. This record changes no normative text.

Use this file for verified delivery status. Use [ROADMAP.md](ROADMAP.md) for
the dated decisions and historical or future milestone ordering within its
stated profile. Older roadmap snapshots, research findings and open issues
must be checked against the delivered source before becoming new work.
External operator handoffs can carry build and deployment evidence; their
historical "next" or "active" paragraphs do not independently authorize a task.

Recovery/journaling and reconnect are future-profile work, not prerequisites
for the delivered casual v1 game. Server-authoritative real-money play is a
separate future product. Relay fallback, another engine integration and wider
platform support also need their own approved scope.

## Work selection and review

Adam subsequently approved remediation of all 212 entries from the
historical audit on October 27. Each entry must be checked against current
source before a fix or resolution is claimed; already-fixed behavior,
approved policy exceptions and historical designs must be recorded honestly.
The remediation preserves the modular core and approved casual rules. It
does not activate a new poker feature, relay fallback or friends beta.
AI Pair reliability remains a separate approved tooling milestone.

The old issue #58 / PR #52 AI Pair profile is already merged. AI Pair retired
that profile and reconciled its queue on October 27; automatic work remains
stopped. Signing identity, explicit resources,
native crypto and casual clocks were delivered by the PRs above. The old
public read-model and protocol-handshake requests are deferred for fresh
design against the current embedding architecture; they are not marked done.

The comprehensive audit completed on October 26 covers historical SHA
`53261f2959451f61b8fbd32fddf189563ba06c71`. Its findings remain historical
evidence and require revalidation against this newer delivered baseline.
Completion of that audit does not claim that current main has been reviewed,
and does not by itself authorize fixes. Adam's subsequent approval supplies
the authority for the current-code remediation described above.

The first current-source fix, [PR #75](https://github.com/nephrium83/texas-holdem/pull/75),
prevents guest acknowledgements from replacing a host's local identity.
Its source is merged at `d7812ffca5d83ce07af57b7a83da0f74990c44b4` after nine
successful CI checks. It is not yet part of the packaged baseline above.
