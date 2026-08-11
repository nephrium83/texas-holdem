# Two-process host command stall — analytical result

Written after a 456-sample stress campaign failed to reproduce the fault
(300 isolated Linux runs, 96 full-suite Linux runs under contention, 60
local Windows runs; zero occurrences). Sampling was abandoned for
analysis, per the escalation rule.

**Result: the single-queue starvation hypothesis is refuted.** Peer
traffic cannot indefinitely delay a queued stdin command, and no
reachable indefinite-blocking cycle exists on the path the host executes
before `start_hand`. The captured state is therefore *not* explained by
the queue, and the search moves to the stdin stages.

No code is changed by this document.

---

## 1. The invariant to explain

From the one CI capture (PR #22 watchdog, 3.10):

| | host | guest |
|---|---|---|
| alive | yes | yes |
| exit code | none | none |
| `start_hand` ack | **no** | yes |
| phases seen | **none** | `dealing` |
| snapshots | **none** | present |
| events total | 4 | 6 |
| stderr bytes | 0 | 0 |

The host was handed a `start_hand` on stdin — written and flushed by the
parent — and never acknowledged it, while the guest processed the
identical command.

---

## 2. State transitions, command write to ack

```
[parent] proc.stdin.write(json) + flush()
    │
    │  (A) OS pipe, parent → child
    ▼
[child] sys.stdin  ──(B)──▶  _stdin_reader thread
    │                            │
    │                            │ (C) json.loads
    │                            ▼
    │                        evt_queue.put(("cmd", …))     ← unbounded
    │                            │
    │                            │ (D) FIFO wait
    ▼                            ▼
[child] main loop: evt_queue.get(timeout=60)
    │
    │  (E) dispatch
    ▼
session.start_p2p_hand(**args)
    │
    │  (F) synchronous work: driver start, _flush_deal, broadcast
    ▼
_emit({"type":"ack","op":"start_hand"})      ← (G) stdout write + flush
```

The same queue also carries `("msg", peer_id, msg)` items pushed by the
per-peer reader thread, so peer traffic and stdin commands share one
consumer.

---

## 3. Per-stage blocking analysis

| # | Stage | Can block? | For how long | Released by |
|---|---|---|---|---|
| A | parent → child pipe | yes | until pipe drains | child reading stdin. Not reached here: the host's stdin carried one command, pipe was empty |
| B | `for line in sys.stdin` | yes | until a newline arrives | data arrival. `TextIOWrapper.__next__` calls `readline()`, which returns on the first newline and does **not** wait to fill a buffer |
| C | `json.loads` | no | O(len) | — |
| D | `evt_queue.put` | **no** | — | queue is unbounded (`queue.Queue()` with no maxsize) — a put can never block |
| D′ | FIFO position | yes | Σ processing time of items ahead | consumer draining them |
| E | `evt_queue.get(timeout=60)` | bounded | ≤ 60 s | item arrival or timeout → `idle timeout` and loop exit |
| F | handler body | **depends — see §4** | | |
| G | `_emit` → stdout write/flush | yes | until parent's pipe drains | `EventCollector._read`, a daemon thread doing `for line in proc.stdout` |

---

## 4. Can peer traffic starve the command? No.

Three properties settle it.

**The queue is FIFO and unbounded.** A `put` never blocks, and items are
consumed in arrival order. A command enqueued at position *k* is reached
after exactly the *k−1* items ahead of it are processed. There is no
priority inversion and no re-ordering.

**The producer is bounded.** The guest emits a finite number of messages
per hand. It cannot replenish the queue indefinitely faster than the
consumer drains it, so there is no livelock.

**Crucially, the messages ahead of the command are not processed at all.**
`Session.__init__` sets `self._hand_no = 0` ("0 = none begun"). The guest
starts its hand first and tags every deal message `hand=1`.
`_hand_msg_ok` then takes this branch:

```python
if h > self._hand_no:            # 1 > 0
    self._msg_buffer.append((conn_id, buffered))
    return False
```

`_on_deal_message` returns immediately on that `False`. The deal driver
is never entered, `_flush_deal` is never called, and therefore **no
broadcast and no `sendall` happens** on the host before `start_hand` is
dispatched. Handling each queued peer message is an O(1) list append.

Measured rather than argued. A host `Session` at `_hand_no = 0`, fed
2000 `deck_round` messages tagged `hand=1` through the real
`handle_message` entry point, with a transport spy counting every send:

```
pre-hand _hand_no : 0
buffered          : 2000
transport calls   : 0        <- no broadcast, no sendall
deal driver       : False    <- never entered
2000 msgs         : 5.9 ms (3.0 us each)
```

Delay is therefore bounded by (messages ahead) × 3 µs. Starving a
command for 48 seconds would need roughly 16 million queued messages
from a peer that sends a few dozen per hand.

> A measurement note for anyone repeating this: `hand` and `seat` must
> sit **inside** `payload`. `handle_message` routes deal types through
> `_hostless_body`, which unwraps the envelope and discards top-level
> fields, so a probe with `hand` at the top level silently defaults to
> the local `_hand_no`, takes the equal branch, and measures a different
> path. That cost one wrong run here.

> **An indefinite stall requires something in the currently executing
> path to block. Volume alone cannot produce one.**

That is the direct answer to the question this goal posed.

---

## 5. Could something in the pre-command path block anyway?

Only three primitives on that path can block unboundedly. Each is
reachable in principle; none is reachable in the captured state.

**`sendall` on a peer socket.** Reached pre-hand only via
`_on_player_info` (host replies `player_ack`, then `_broadcast_player_list`).
It blocks only if the guest stops draining its socket. The guest drains
on a dedicated reader thread into an unbounded queue, so its receive
window is emptied regardless of what its main loop is doing.

Note also that `broadcast()` and `send()` snapshot the socket list under
`_writers_lock` and then call `sendall` **outside** the lock:

```python
with self._writers_lock:
    socks = list(self._peers.values())
for s in socks:
    s.sendall(data)
```

So a slow write cannot block `_handshake`'s peer registration. The
obvious lock-held-during-IO deadlock does not exist here.

**`_emit` → `sys.stdout.write` + `flush`.** Blocks if the parent's
stdout pipe fills (~64 KiB) and stays full. The parent drains it on a
daemon thread. Decisively, the captured host had emitted **4 events and
zero snapshots** — it was nowhere near filling a pipe.

**`evt_queue.get`.** Bounded at 60 s by construction, and expiry emits
`idle timeout` and exits the loop rather than hanging.

**A distributed `sendall` deadlock** — host blocked writing to guest
while guest is blocked writing to host — is the classic shape and is
structurally excluded here: both processes drain their sockets on
dedicated threads into unbounded queues, so neither receive window can
stay full. It would become reachable if either reader thread were ever
merged into the main loop.

---

## 6. Consequence: the stall is upstream of the queue

Stages D through G are excluded for the captured state. By elimination
the command did not reach the consumer, which places the fault in
**A, B, or C** — the stdin path — or in the reader thread's existence.

PR #25's classifier separates exactly these:

```
no stdin_open               -> reader thread never started
stdin_open, no stdin_line   -> blocked on read, or bytes never arrived
stdin_line, no cmd_queued   -> read but never parsed/queued
cmd_queued, no cmd_dispatch -> queued, consumer never reached it   [REFUTED above]
cmd_dispatch, no ack        -> dispatch began and blocked
```

The capture predates that classifier, so no evidence distinguishes A/B/C
today. This analysis says the fourth line is the one we can now stop
looking at.

One structural observation worth recording for whoever picks this up:
`main()` starts the stdin reader **only after** `transport.listen()`,
`wait_connected(timeout=15.0)`, a `_peers` read, and `_emit("connected")`.
The parent begins sending commands as soon as it observes `connected`.
Bytes written in that window sit in the pipe — harmless if the thread
starts, fatal if it does not. That is stage A→B, and it is host-only:
the guest reaches the same point via `connect()` rather than
`listen()` + accept.

---

## 7. On the Linux-only pattern

All three observed occurrences were Linux CI; none locally on Windows.
That is **not** established as causal, and this analysis found no
platform-specific mechanism to justify it. Alternative explanations that
fit equally well: Windows runs are far fewer, and local runs are
unloaded. Treating "Linux-only" as a clue before a mechanism is
identified would be reading a sample of three.

---

## 8. What was not done, and why

No fix is proposed. Criteria 4 through 8 of the goal are conditional on
identifying a candidate mechanism; §4 refutes the one that motivated
them, and §5 finds no replacement. Building a "smallest deterministic
reproduction" of a mechanism that has not been identified would mean
reproducing the outward signature again — which
`test_starved_consumer_is_classified_not_guessed` already does, and
which is explicitly not evidence for the production cause.

The next real datum is a CI occurrence carrying a `command_path` line.
