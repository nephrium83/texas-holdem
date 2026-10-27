# Current delivery

Verified October 27, 2026. This is a delivery and coordination record. It does
not change poker rules, protocol contracts, or authorize another feature.

## Delivered baseline

GitHub main is `32a9966175d604ff3f5fe004dcd76070193f7d62`, the merge of
[PR #74](https://github.com/nephrium83/texas-holdem/pull/74). Recheck main before
starting new work; this SHA records what was verified here.

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

The current approved tooling milestone is AI Pair reliability. This record
does not activate a new poker feature or a friends beta. A subsequent delivery
task needs a fresh main SHA, a concrete owner-approved goal and acceptance gate.

The old issue #58 / PR #52 AI Pair profile is already merged. Its queue must
be reconciled instead of resumed. Signing identity, explicit resources,
native crypto and casual clocks were delivered by the PRs above. The old
public read-model and protocol-handshake requests are deferred for fresh
design against the current embedding architecture; they are not marked done.

The comprehensive audit completed on October 26 covers historical SHA
`53261f2959451f61b8fbd32fddf189563ba06c71`. Its findings remain historical
evidence and require revalidation against this newer delivered baseline.
Completion of that audit does not claim that current main has been reviewed,
and it does not authorize automatic poker fixes.
