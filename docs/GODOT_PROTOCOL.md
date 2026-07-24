# Godot ↔ Sidecar Protocol

This is the contract the **Godot client** speaks to the **Python sidecar**.
It is the source of truth for building the client. The Python reference
implementation of every message in this document is `holdem/client_view.py`
(snapshots + command handling) and `holdem/contract.py` (the per-seat view
it builds on); both are covered by `tests/test_client_view.py`. If this
document and that code ever disagree, the code is authoritative and this
document is the bug.

Status: v1. The message schemas mirror tested code. The transport framing
(§2) and connection lifecycle (§7) are implemented by the sidecar's client
server (`holdem/client_server.py`, `tests/test_client_server.py`) and are
now locked.

---

## 1. Architecture — why a sidecar

The player runs the **Godot app**. Godot renders and takes input; it runs
**no game logic and holds no secrets**. Alongside it runs a **Python
sidecar** process that owns the `Session`: the mental-poker crypto, the
peer-to-peer networking to the other players, and the local replica engine
that computes betting. Godot talks only to its own local sidecar; the
sidecar talks to the other players' sidecars over the internet.

```
  [ Godot client ] <--localhost, JSON--> [ Python sidecar ] <--P2P--> [ other players' sidecars ]
     render + input                        Session: crypto,
     (no logic, no keys)                   P2P, replica betting
```

Why the split, and why it is not going to change: the anti-cheat crypto
(Ristretto255 over libsodium, threshold ElGamal, DLEQ proofs, shuffle
proofs, the post-hand audit) is heavy and security-critical. Reimplementing
it in GDScript would be a large effort and a liability. The sidecar is the
already-built, already-tested `Session`; Godot is a thin front end.

The single most important security property the sidecar enforces on the
client's behalf: **a client is only ever sent the hole cards for its own
seat** (until a showdown makes them public). A compromised or modified
client cannot leak what it was never given.

---

## 2. Transport and framing  *(locked — implemented)*

The sidecar listens on a **localhost TCP socket** (default `127.0.0.1`, a
port the client is told at launch). Messages are **UTF-8 JSON objects, one
per line, terminated by `\n`** (newline-delimited JSON). No length prefix,
no additional framing. Both directions use the same framing.

Godot side: `StreamPeerTCP`, read to the next `\n`, `JSON.parse_string`.

WebSocket was considered and passed over: the sidecar serves plain TCP +
newline-JSON (`holdem/client_server.py`). The message bodies in §4–§6
would carry over unchanged if a WebSocket listener were ever added
alongside; nothing in the schemas assumes the transport.

Every message is a JSON object with a `"type"` field. Unknown message types
must be ignored by both sides (forward compatibility).

---

## 3. Card encoding

A card is a string: **rank followed by suit letter**.

- Ranks: `2 3 4 5 6 7 8 9 10 J Q K A`. **Ten is `"10"`, not `"T"`.**
- Suits: `c` d` h` s` (clubs, diamonds, hearts, spades), always lowercase.

Therefore a card string is **two or three characters**: `"Ah"`, `"Ks"`,
`"7c"`, `"10s"`, `"10d"`. Parse it as: **suit = last character, rank =
everything before it.** Do not assume a fixed length.

The board is an array of card strings, length 0 to 5.

---

## 4. Client → sidecar: commands

The client sends a command object. Only the betting commands are defined in
v1; lobby/seating commands are §8 (not yet finalised).

```json
{ "type": "command", "command": "<name>", "payload": { ... } }
```

| command      | payload            | meaning                                        |
|--------------|--------------------|------------------------------------------------|
| `fold`       | (none)             | fold the current hand                          |
| `check_call` | (none)             | check if nothing is owed, otherwise call       |
| `raise_to`   | `{"amount": <int>}`| raise so your total this street becomes amount |
| `next_hand`  | (none)             | advance after a settled or voided hand         |

`amount` in `raise_to` is an **absolute target** (your total wagered on this
street after the raise), not a delta. Legal bounds are given to you in the
snapshot (`you.legal.min_to` / `max_to`).

The sidecar validates every command exactly as it validates a remote peer's
action — an out-of-turn or illegal command is **rejected, never trusted**.
The client should only enable a control when the snapshot says it is legal,
but must handle rejection gracefully regardless.

### Command result

For each command the sidecar replies with:

```json
{ "type": "command_result", "command": "fold", "ok": true, "verdict": "applied" }
```

- `ok`: `true` iff the action was applied.
- `verdict`: `"applied"` | `"rejected"` (not your turn / illegal) |
  `"buffered"` (accepted but queued behind an earlier action) |
  `"stale"` (a duplicate). On an unknown command, `ok` is `false` and an
  `"error"` string is present instead of `verdict`.

For `next_hand`, `verdict` is one of:

- `"started"` — the next hand is underway.
- `"not_ready"` — the current hand has not settled or voided.
- `"eliminated"` — this seat is busted and no longer participates in deals.
- `"session_over"` — the match has ended.

`ok` is true for `started`, `eliminated`, and `session_over`.

A fresh **snapshot** (§5) is sent immediately after the command result, so
the client can render from that rather than mutating local state itself.

---

## 5. Sidecar → client: snapshot

The core message. The sidecar sends a snapshot on connect, after every
command, and **unprompted whenever game state changes** (a remote player
acted, the deal progressed, the hand settled). The client re-renders from
the latest snapshot; it never advances state on its own.

```json
{
  "type": "snapshot",
  "seat": 0,
  "phase": "betting",
  "hand_num": 7,
  "street": "flop",
  "board": ["2c", "7d", "10h"],
  "pot": 90,
  "button": 2,
  "sb_seat": 0,
  "bb_seat": 1,
  "action_on": 0,
  "voided": false,
  "void_reason": null,
  "result": null,
  "seats": [
    { "seat": 0, "name": "Ada", "stack": 455, "bet": 0, "folded": false,
      "all_in": false, "in_seat": true, "sitting_out": false,
      "last_action": "", "pos": "SB", "is_you": true },
    { "seat": 1, "name": "Ben", "stack": 480, "bet": 0, "folded": false,
      "all_in": false, "in_seat": true, "sitting_out": false,
      "last_action": "CHECK", "pos": "BB", "is_you": false }
  ],
  "you": {
    "hole": ["Ah", "Kd"],
    "legal": { "to_call": 0, "can_check": true, "can_raise": true,
               "min_to": 20, "max_to": 455, "pot": 90 }
  }
}
```

### Top-level fields

| field         | type            | notes                                                        |
|---------------|-----------------|--------------------------------------------------------------|
| `seat`        | int             | the local player's own seat index                            |
| `phase`       | string          | see phase table below                                        |
| `hand_num`    | int             | which hand this is                                           |
| `street`      | string          | `preflop` `flop` `turn` `river` `showdown`, or `idle` between hands |
| `board`       | array of card   | 0–5 community cards                                           |
| `pot`         | int             | total in the pot                                             |
| `button`      | int             | seat index of the button (post dead-button move)             |
| `sb_seat`     | int             | small-blind seat index                                       |
| `bb_seat`     | int             | big-blind seat index                                         |
| `action_on`   | int             | seat index to act, or `-1` if nobody is to act               |
| `voided`      | bool            | hand was voided (cheat/desync/dropout); chips reverted       |
| `void_reason` | string \| null  | human-readable reason when `voided`                          |
| `result`      | object \| null  | settlement result when `phase` is `settled` (see §6)         |
| `session_over`| bool            | true once at most one seat has chips                         |
| `session_winner` | int \| null  | winning seat when `session_over`; null for no winner          |
| `eliminated`  | bool            | local seat is busted and excluded from later deals            |
| `final_stacks`| array \| null   | final stack by seat once `session_over`                       |
| `seats`       | array           | one entry per seat, in seat order                            |
| `you`         | object          | data private to the local seat                               |

### `phase`

| phase      | meaning                                                                  |
|------------|--------------------------------------------------------------------------|
| `lobby`    | no hand in progress; `seats` lists table membership, `you.seat` is yours |
| `dealing`  | the mental-poker deal is running; cards not yet in hand — show a spinner  |
| `betting`  | a betting round is open                                                  |
| `settled`  | the hand is over and paid out; `result` is populated                     |
| `void`     | the hand was aborted; `void_reason` says why; chips are as before the hand |

### Continuous-session lifecycle

After `settled` or `void`, the client sends `next_hand`. Every participating
sidecar derives the next hand from its identical replica: settled stacks carry
forward, while a void redeals from the current hand's original stacks and
position chain.

A busted sidecar becomes a lightweight spectator. It no longer participates in
the mental-poker deal and ignores later hand traffic, but remains subscribed to
the signed match lifecycle. When the final hand ends, `session_over`,
`session_winner`, and `final_stacks` are pushed to active and eliminated
clients alike.

Any authenticated peer may fail the current hand closed. A locally detected
deal failure or replica desync broadcasts an idempotent signed hand-void
message; every current participant enters `phase: "void"` and uses the same
redeal inputs. In an n-of-n protocol, a malicious peer can already halt by
disconnecting, so v1 favors safety and attribution over trying to continue a
possibly divergent hand.

### `seats[i]`

Public, per-seat, **never contains hole cards during play**.

| field         | type          | notes                                             |
|---------------|---------------|---------------------------------------------------|
| `seat`        | int           | seat index                                        |
| `name`        | string        | display name                                      |
| `stack`       | int           | chips behind                                      |
| `bet`         | int           | chips wagered on the current street               |
| `folded`      | bool          |                                                   |
| `all_in`      | bool          |                                                   |
| `in_seat`     | bool          | dealt into this hand                              |
| `sitting_out` | bool          |                                                   |
| `last_action` | string        | e.g. `"CALL 20"`, `"RAISE 60"`, `"CHECK"`, `""`   |
| `pos`         | string \| null| `"BTN"` `"SB"` `"BB"` or null                      |
| `is_you`      | bool          | true for the local seat                           |
| `hole`        | array of card | **present only at a contested showdown** (§6)     |

### `you`

Private to the local seat.

| field   | type          | notes                                                                    |
|---------|---------------|--------------------------------------------------------------------------|
| `hole`  | array of card | your two hole cards. **Absent** while `phase` is `dealing` (not yet dealt).|
| `legal` | object        | **present only when it is your turn to act** — see below. Absent otherwise.|

`you.legal` (present iff `action_on == your seat` and `phase == betting`):

| field        | type | notes                                              |
|--------------|------|----------------------------------------------------|
| `to_call`    | int  | chips needed to call (0 means you may check)       |
| `can_check`  | bool |                                                    |
| `can_raise`  | bool |                                                    |
| `min_to`     | int  | minimum absolute target for `raise_to`             |
| `max_to`     | int  | maximum absolute target (your whole stack all-in)  |
| `pot`        | int  | pot size (for pot-limit / bet-sizing UI)           |

The presence of `you.legal` is the client's cue that it is this player's
turn: enable Fold / Check-Call / Raise, using `to_call`, `can_check`,
`can_raise`, and the `[min_to, max_to]` slider bounds.

---

## 6. Settlement result and showdown reveals

When `phase == "settled"`, the top-level `result` object describes the
payout. It is produced by the engine's `settle()` and normalised to plain
JSON (sets become sorted arrays, cards become `[value, suit_index]` pairs,
value 2–14, suit index 0–3). Shape (fields the client will use most):

```json
"result": {
  "pots": [ { "amount": 120, "eligible": [0, 1, 2] } ],
  "winners": [0],
  "runs": [ { "board": [...], "scores": {...}, "best": {...} } ],
  "refund": null,
  "tabled": true
}
```

- `winners`: seat indices that won chips.
- `pots`: each pot with its amount and eligible seats (main pot first, then
  side pots).
- `runs`: non-empty for a **contested showdown** (two or more players saw it
  through); empty for a fold-out.

**Showdown reveals.** At a contested showdown (`result.runs` non-empty) the
post-hand audit has already made every player's cards public, so each entry
in `seats` for a still-in player carries its `hole`. Table those cards. A
hand that ended by folds (`result.runs` empty) reveals **no** `hole` fields —
the winner is not shown, exactly as at a real table.

---

## 7. Connection lifecycle  *(implemented)*

As implemented by `holdem/client_server.py`:

1. Sidecar starts, begins its P2P work, and listens on the local port.
2. Client connects. Sidecar sends `{"type":"hello","protocol":1}` then an
   initial `snapshot` (likely `phase: "lobby"`).
3. Steady state: the client sends `command` messages; the sidecar answers
   with `command_result` + `snapshot`, and also pushes a `snapshot`
   whenever remote events change the game.
4. On disconnect the client may reconnect and will receive a fresh
   `hello` + `snapshot` reflecting current state.
5. An eliminated client may reconnect to its retained hand view and later
   receives the terminal winner through a pushed snapshot.

Not yet specified: how a player joins/creates a table, the seating/ready
handshake, reconnection identity, and chat. These live in `MULTIPLAYER.md`
(the P2P lobby layer) and will be surfaced to the client as additional
message types.

---

## 8. Not yet in this contract

- Lobby / table creation / join / ready / seat selection (P2P lobby exists
  in the sidecar; not yet exposed as client messages here).
- Chat.
- Mid-hand dropout **timeout** (the void path exists; the timer for a peer
  that simply goes silent is not yet wired).
- Rebuys / sit-out / sit-in commands (engine supports them; not yet exposed).

These will be added as message types without breaking §4–§6.

---

## 9. Reference implementation

- `holdem/client_view.py` — `snapshot(session)` produces §5/§6; 
  `apply_command(session, command, payload)` consumes §4.
- `holdem/contract.py` — `build_snapshot(engine, seat)` (the public per-seat
  view) and `card_str` (§3 encoding).
- `tests/test_client_view.py` — exercises the no-leak invariant, the phase
  transitions, showdown reveals, and command verdicts over real hands.
- `holdem/client_server.py` — the localhost server itself: §2 framing,
  §7 hello, §4 command handling, §5 unprompted pushes (coalesced, via
  `Session.on_state_changed`).
- `tests/test_client_server.py` — the whole protocol over a real socket
  against live hostless hands on the in-memory bus.

Build the Godot client against these. When in doubt, run a sidecar and read
the actual JSON.
