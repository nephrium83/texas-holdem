# Peer Timeout Behavior — Contract Specification

**Status:** Pre-implementation contract. This document is the specification.  
**Phase:** 3 — Deterministic silent-peer timeout  
**Branch:** agent/sidecar-integration

---

## Purpose

This document defines the complete timeout behavior for every phase of a
hostless peer-to-peer session. It exists to prevent the implementation from
quietly becoming the specification. All edge cases are resolved here.
Implementation that contradicts this document is a bug in the implementation.

---

## Core Abstractions

### DeadlineToken

A frozen value that uniquely identifies one expected action from one peer in
one phase of one hand. Stale proposals from an earlier phase or sequence
cannot match the current token.

```python
@dataclass(frozen=True)
class DeadlineToken:
    hand_id: str        # e.g. "poker.deal.v2:<sha256 hex>"
    phase: str          # "betting", "deal_shuffle", "deal_decrypt", etc.
    actor: str | None   # conn_id of the awaited peer; None for multi-peer phases
    action_seq: int     # replica's next_action_seq at time of proposal
```

`hand_id` is `session._deal_session_id()`, which encodes the seat order and
is stable for the lifetime of a hand. Using it prevents a proposal from one
hand being replayed in the next hand with the same sequence number.

`phase` prevents a stale betting-phase proposal from matching a deal phase
with the same sequence number. Phase strings must match the replica's current
phase exactly (see Phase Matrix below for canonical strings).

`actor` is the conn_id of the peer whose contribution is awaited, or `None`
for phases where any missing peer terminates the phase (e.g. shuffle, where
all peers must contribute and the first non-responder determines the timeout).
When `actor` is `None`, the proposal additionally carries `missing_seat: int`.

`action_seq` is the replica's `next_seq` value at the moment the proposer
decides to broadcast. This anchors the proposal to a specific point in the
action log. A proposal with a lower seq than the current replica seq is stale
and must be rejected without side effects.

### Clock Protocol

```python
class Clock(Protocol):
    def monotonic(self) -> float: ...
```

`Session.__init__` accepts an optional `clock: Clock` parameter. The default
is a thin wrapper around `time.monotonic`. Tests inject a fake clock that
advances only when told to.

The clock is used **only** to decide when a peer is allowed to broadcast a
`timeout_proposal`. It plays no role in whether a received proposal is
accepted or rejected. Divergent wall clocks on different machines cannot
cause divergent replica transitions.

### Timeout Duration

Configurable via table settings, with per-phase defaults:

| Phase category         | Default (seconds) |
|------------------------|------------------:|
| Betting action         |              30   |
| Lobby handshake        |              60   |
| Lobby ready            |             120   |
| Deal contribution      |              30   |
| Settlement ack         |              10   |

Duration is replicated as part of table config so every peer uses the same
deadline arithmetic. The Session tracks `_deadline_started_at: float | None`
(wall time) and `_current_deadline_token: DeadlineToken | None`.

---

## Timeout Proposal Lifecycle

### 1. Starting a deadline

When a peer transitions to a state requiring another peer's action, it:

1. Records the current wall time as `_deadline_started_at`.
2. Constructs a `DeadlineToken` from the replica's current state.
3. Stores it as `_current_deadline_token`.

### 2. Deciding to propose

On each periodic poll (or on an event triggering a re-check), the peer:

```python
if clock.monotonic() - _deadline_started_at > phase_timeout:
    broadcast(timeout_proposal(token=_current_deadline_token))
```

Any peer may broadcast a proposal. Multiple simultaneous proposals for the
same token are idempotent — they resolve to a single replica transition.

### 3. Receiving and validating a proposal

```python
def _on_timeout_proposal(self, conn_id: str, msg: dict) -> None:
    token = DeadlineToken(**msg["token"])
    # Reject if not in an active hand
    if self._replica is None or self.hand_voided:
        return
    # Reject if token does not match current deadline
    if token != self._current_deadline_token:
        _log.debug("stale timeout proposal from %s — dropping", conn_id)
        return
    # Reject if action seq has already advanced past the proposal
    if token.action_seq != self._replica.next_seq:
        _log.debug("out-of-order timeout proposal from %s — dropping", conn_id)
        return
    # Apply phase-specific timeout
    self._apply_timeout(token)
```

### 4. The action-vs-proposal race

The critical race:

1. Peer A is the actor. Their wall clock is slow or their network is fast.
2. Peers B and C decide the deadline has passed and broadcast a proposal.
3. Peer A's action and the proposals arrive at different peers in different orders.

**Resolution:** the replica's `action_seq` decides the winner.

- If Peer A's action arrives at the replica first (i.e., `replica.apply_action`
  succeeds and advances `next_seq`), the subsequent proposal carries a stale
  `action_seq` and is rejected by step 3 above.
- If the proposal is applied first (advancing replica state via the timeout
  transition), Peer A's action arrives with a stale or wrong-actor seq and is
  rejected by the existing action validation.
- If both a proposal and an action share the same `action_seq`, the
  **proposal is rejected in favour of the action**. The action is applied
  first by the replica; the proposal arrives stale.

This is deterministic because every peer applies transitions in sequence-number
order and the digest check (`state_digest()`) catches any divergence.

### 5. Idempotency

A timeout transition must be idempotent at the replica level: applying it
twice must produce the same state as applying it once. This is guaranteed by
the seq validation — once the transition advances `next_seq`, subsequent
proposals with the old seq are stale and dropped.

---

## Phase Matrix

The table below is exhaustive. Any phase/party combination not listed here
must produce no gameplay effect (log a warning, drop the message).

| Phase | Awaited party | Canonical phase string | Timeout result | Stack preservation |
|---|---|---|---|---|
| Lobby — initial connect | Joining peer | `"lobby_handshake"` | Remove peer from pending list; do not seat | N/A |
| Lobby — ready check | Seated peer | `"lobby_ready"` | Mark peer unready; host may remove or wait | N/A |
| Betting — facing a bet | Current actor | `"betting"` | **Fold** actor's hand | Yes |
| Betting — no bet faced (check option) | Current actor | `"betting"` | **Check** on actor's behalf | Yes |
| Deal — shuffle contribution | Required peer (all) | `"deal_shuffle"` | Abort hand; void with reason; stacks unchanged | Yes |
| Deal — decryption contribution | Required peer (all) | `"deal_decrypt"` | Abort hand; void with reason; stacks unchanged | Yes |
| Settlement acknowledgment | Required peer | `"settlement_ack"` | Continue if result is already deterministic; abort and preserve stacks otherwise | Yes |
| Observer silence | Observer (busted out) | N/A | Disconnect locally; no gameplay effect | N/A |
| Non-acting seated peer | Non-actor | N/A | **No timeout.** Non-actors do not hold the game. | N/A |

### Fold vs. Check distinction

The replica must distinguish "facing a bet" (call/fold/raise decision) from
"not facing a bet" (check/bet decision) to select the correct default action.
This is available from `replica.engine.legal(actor)["to_call"]`:

- `to_call > 0` → actor is facing a bet → timeout applies **Fold**
- `to_call == 0` → actor has no bet to face → timeout applies **Check**

The timeout path calls `replica.apply_action(seq, actor_seat, action, 0)` with
the derived action, then broadcasts `bet_action` as if the actor had acted
normally, carrying the post-apply digest. All peers validate the digest; any
divergence triggers the existing desync-void path.

### Cryptographic contribution failures

Betting timeouts have a safe default poker semantics. A missing shuffle or
decryption contribution does not: the hand cannot be completed without every
peer's cryptographic share. The correct behavior is:

1. Void the hand (`hand_void` message with `reason="timeout: deal_shuffle"` or
   `reason="timeout: deal_decrypt"` and `seat=<missing_seat>`).
2. Preserve all stacks at their pre-hand values (existing void-and-redeal path).
3. Mark the timed-out peer as `unavailable=True` on its `Player` record.
4. Host broadcasts an updated player list; all peers can decide locally whether
   to continue without that peer or end the session.

The peer is not automatically removed. The host UI (or the Godot client) must
present the "peer unavailable" state and let the human decide.

### Settlement acknowledgment

If the hand result is mathematically determined (all-but-one folded; showdown
with deterministic winner) and every peer's replica has already committed the
same `hand_result`, silence during settlement ack does not prevent progress —
the result is applied from local state. If the result requires information that
has not arrived (e.g. an outstanding audit share at showdown), the hand is
voided with stacks preserved.

---

## Wire Format

`timeout_proposal` follows the existing hostless message conventions: it is
signed via the transport layer and carries a hand number for `_hand_msg_ok`
filtering.

```json
{
  "type": "timeout_proposal",
  "hand": 3,
  "token": {
    "hand_id": "poker.deal.v2:3f7a...c1",
    "phase": "betting",
    "actor": "peerA",
    "action_seq": 7
  },
  "missing_seat": null
}
```

`missing_seat` is an integer seat index when `actor` is `None` (multi-peer
phases); it is `null` for single-actor betting timeouts.

The message is added to `_HOSTLESS_PAYLOAD_TYPES` so `_hostless_body` unwraps
it correctly for the Session dispatcher.

---

## What Is Not In Scope

The following are explicitly deferred:

- **Automatic peer removal.** The host marks a peer unavailable; the human
  decides whether to continue.
- **Reconnection / grace period.** A timed-out peer does not get a second
  chance within the same hand. Future hands may re-seat them if the session
  continues.
- **Cascading timeouts.** If the timeout transition results in a new actor who
  also times out, that is handled by a fresh deadline cycle, not by the
  original proposal.
- **Lobby timeout for the host.** If the host disconnects, the existing host
  migration path applies; timeout is not involved.

---

## Definition of Done

- [ ] `DeadlineToken` dataclass exists and is used everywhere a deadline is set
- [ ] `Clock` Protocol is injectable at Session construction; tests use a fake
- [ ] `timeout_proposal` message type is signed, transported, and dispatched
- [ ] `_on_timeout_proposal` validates token, seq, and phase before applying
- [ ] Phase matrix above is fully covered by the implementation
- [ ] Betting timeout: fold (facing bet) or check (no bet) applied via replica
- [ ] Deal contribution timeout: hand voided, stacks preserved, peer marked unavailable
- [ ] Race condition: action-beats-proposal and proposal-beats-action both tested
- [ ] No test relies on `time.sleep` or real wall time
- [ ] All peers converge on the same post-timeout state (digest check)
- [ ] Late messages are rejected after the timeout transition
- [ ] 386+ tests pass; Godot sidecar integration remains green
- [ ] Existing wire protocol snapshots remain compatible (no breaking changes)
