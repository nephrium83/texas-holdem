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

The invite codec's socket conversion is the sole remaining import-boundary
exception. Libsodium initialization and other planned core-boundary work
remain separate slices.
