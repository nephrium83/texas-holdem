# Explicit session resources and clocks

Session receives its transport, clock and secret source from its host.
It never selects the process transport, creates a real clock, or imports
the device-secret file adapter. The core boundary test removes those two
remaining Session exceptions.

## Host composition

```python
from holdem.p2p import device_secret, transport
from holdem.p2p.session import Session

session = Session(
    is_host=False,
    nickname="Player",
    avatar_b64="",
    transport=transport,
    clock=transport.get_clock(),
    secret_provider=device_secret.load_or_create,
)
```

Admission and host-key pinning remain separate required parts of the
networked startup flow; the example only illustrates resource selection.
The desktop host and join paths supply their existing admission objects
before any socket is opened. The local sidecar supplies its in-memory
transport, a shared real clock, and the same lazy device-secret adapter.

`transport` and `clock` are required; explicit `None` is rejected. Supply
either `master_secret=bytes` or a callable `secret_provider`. A provider is
not invoked during construction or lobby setup. The first key-share demand
loads it; a successful result is cached, and a failed call can be retried.
An explicit master secret or the existing `_deal_master_secret` setter
takes precedence and bypasses the provider.

## Time sources

The supplied clock has two methods:

| Method | Use |
| --- | --- |
| `monotonic() -> float` | Gameplay deadlines and hand/session forensic records |
| `time() -> float` | Event timestamps and signed wire timestamps/freshness |

`RealClock` adapts the system sources. `FakeClock(start=0,
wall_start=None)` supports independent initial values. `advance(seconds)`
moves both sources; `advance_wall(seconds)` moves only wall time, allowing
clock-jump tests without advancing a gameplay deadline.

Both `wire.pack(..., signer=..., clock=...)` and `wire.unpack(raw,
clock=...)` require an explicit clock. `JoinAuthenticator` also receives
the clock used to sign player information. The desktop transport exposes
`get_clock()` so Session, player-information signing, GUI sends, Ready, and wire
verification use the same host clock adapter.

Admission's challenge TTL retains its existing separate monotonic callable;
this slice does not change that timer.

The wire version, field layout, canonical signing/hash inputs, millisecond
conversion, and inclusive plus/minus 30-second freshness window are
unchanged. The existing golden envelope bytes remain pinned. This change
does not alter timeout application, settlement, disconnect handling,
heartbeat timing, or the table's poker rules.

## Integration

This slice is based on the identity refactor in PR #59 (`733abbd`). It is
kept on `refactor/session-dependencies` so it can be reviewed separately.

PR #52 was inspected at `9623ff9`. Its dependency initialization is
unchanged, while it adds settlement state and rewrites timeout behavior
and the peer test helper. This slice changes dependency selection and
timestamp sources only. Combining the branches requires migrating that
helper and the revised timeout tests to the explicit inputs; it must
retain #52's gameplay decisions.

A read-only merge simulation of the implementation snapshot `e4c430d`
and #52's `9623ff9` auto-merges Session production code. It identifies four
conflict paths:

- `holdem/p2p/timeout.py`: combine Clock documentation, retaining the new
  wall-time API and #52's report-only timeout description.
- `tests/prod_peer.py`: retain #52's shipped authenticator flow rather
  than the obsolete manual handshake. Add the signer's public key to
  invite admission and pass the signer/shared clock to the authenticator.
- `tests/test_session_match.py`: retain both the fake-clock and client-view
  imports.
- `tests/test_timeout.py`: retain #52's report-only semantics and migrate
  its two new session factories to explicit secrets. Keep missing-clock
  rejection coverage.

The new `test_peer_loss` factory on #52 also needs an explicit fake clock.
These are integration instructions, not a merge or test of the combined
branches.

## Validation notes

The implementation was committed as `e4c430d` before its five new
deliberate-break controls ran. Those controls detect eager secret loading,
hidden host-resource fallback, wrong event wall time, wrong forensic
monotonic time, and use of system time for a signed wire timestamp.

The actual key-announcement test starts two hands, verifies each public
share against the supplied secret and canonical hand/seat context, and
observes only one provider call with no config directory created.

The first full run exposed a pre-existing chance dependency in
`test_a_seat_busted_before_the_end_reports_the_match_final_stacks`. The
same failure reproduced on identity snapshot `733abbd` on the sixth
independent run: random wins could prevent the assumed short-seat bust.
That test now pins only the shuffle permutations; real encryption,
threshold dealing, audit, betting, and all prior assertions remain. It
passes ten fresh interpreter runs, with the intended bust in hand 1 and
match completion in hand 2.

The invite codec's socket conversion is the sole remaining import-boundary
exception. Libsodium initialization and other planned core-boundary work
remain separate slices.
