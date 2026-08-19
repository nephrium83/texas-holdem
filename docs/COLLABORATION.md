# Collaboration Model

Two AI assistants and one human work on this repository. This document
exists so that technical state never has to be relayed by hand.

## Source of truth

**The repository and its GitHub issues are the source of truth.**

Not chat transcripts. Not artifacts. Not a summary in someone's scrollback.
If a finding matters, it lives in `docs/`, in a commit, or in a comment on
the canonical issue — otherwise it does not exist.

| Surface | Role |
|---|---|
| `docs/ROADMAP.md` | canonical milestone ordering and status |
| `docs/research/` | evidence and candidate designs, clearly marked as research |
| `docs/*_SPEC.md`, `docs/POKER_RULES_PROFILE.md` | normative contracts |
| Canonical GitHub issue | durable handoffs, decisions, current state |
| Chat / artifacts | working surface only — **never** the only home for a finding |

## Roles

**Claude** implements and researches. It must leave a durable handoff on
the canonical issue at the end of every substantive run, and must not
leave important findings only in chat or in an artifact.

**ChatGPT** reviews independently against the actual repository — commits,
diffs, issues, PRs. It should be able to work from a SHA or issue
reference without being handed context.

**Adam** provides issue / PR / SHA references and makes product and merge
decisions. He should not have to relay technical state between the two
assistants.

## Research versus contract

Keep these separate and label them explicitly.

- **Research** (`docs/research/`) records what was measured, what was
  modelled, and what is proposed. A candidate design in a research note
  is not binding.
- **Contract** (`TIMEOUT_SPEC`, `POKER_RULES_PROFILE`) records what the
  implementation must do. Implementation that contradicts a contract is a
  bug in the implementation.

Promoting a candidate design into a contract is a deliberate, reviewed
act. It never happens silently.

## Handoff format

Post this to the canonical issue at the end of every substantive run.

```markdown
## Handoff

Milestone:
Base SHA:
Head SHA:
Branch/worktree:

### Changed
### Evidence
### Findings
### Known limitations
### Blockers
### Decisions required
### Next recommended goal
```

Notes on filling it in:

- **Evidence** means what was actually run and what it showed — control
  results, measured values, failing and passing tests. Not "verified" as
  an assertion.
- **Known limitations** is not optional. If something was scoped out,
  assumed, or left unproven, it goes here.
- **Decisions required** is for things only Adam can settle: product
  behaviour, merge calls, risk acceptance. Do not decide these
  unilaterally and do not bury them.
- **Next recommended goal** should be specific enough to paste straight
  into the next run.

## Conventions carried from prior work

- Every test is verified against a deliberately broken implementation,
  and the control must name which test fires.
- Commit before running controls.
- Commit messages explain the *why*, including what the controls showed.
- Parked or forensic artifacts (`6955f4f`, `6b553a8`, PR #36) are
  preserved exactly. Do not merge or rewrite them.
- Branch from `origin/main`; local `main` may lag.
