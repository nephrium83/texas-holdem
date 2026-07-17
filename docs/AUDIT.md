# Texas Hold'em — Code Audit Report
**Date:** 2026-07-16  
**Repo:** github.com/nephrium83/texas-holdem  
**Audited by:** Static analysis of all source files  

---

## Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 3 |
| HIGH     | 11 |
| MEDIUM   | 12 |
| LOW      | 5 |
| **Total**| **31** |

### Top-5 Priorities

1. **[C-1] Wire signatures are never verified on the receive path** — the entire Ed25519 security model is bypassed; any peer can forge actions or control messages.
2. **[C-2] Admin messages (pause/resume/kick/adjust-blinds) are accepted from any peer** — any connected player can kick others, change stakes, or halt the game.
3. **[C-3] No message-size limit in the TCP transport** — a malicious peer sends a crafted 4-byte length header to trigger a multi-GB allocation and crash the process.
4. **[H-1] `start_host()` announces `0.0.0.0` as the LAN address** — joiners always get a non-routable address and can never connect.
5. **[H-9] `pyproject.toml` omits `holdem.p2p`** — `pip install` silently drops the entire multiplayer stack; the app crashes on import.

---

## 1. Security

### C-1 · CRITICAL — Wire signatures never verified on the receive path
**File:** `holdem/p2p/session.py` line 81, `holdem/p2p/wire.py` line 62  
`wire.pack()` creates a signed Ed25519 envelope, but `transport.py` deserialises each incoming frame with bare `json.loads(body)` and passes the raw dict to `session.handle_message()`. Neither `handle_message` nor any downstream handler ever calls `wire.unpack()`. The signature fields (`sig`, `pubkey`) are stored in the dict but ignored. Any peer can omit them entirely, or set any `pubkey`, and their messages are accepted.  
**Fix:** In `_handle_connection` (transport.py line 150), after `json.loads`, call `wire.unpack(msg)` and drop the message if it raises. Alternatively, call `wire.unpack` at the top of `session.handle_message`.

---

### C-2 · CRITICAL — Admin messages accepted from any peer
**File:** `holdem/p2p/session.py` lines 236–250  
`_on_pause`, `_on_resume`, `_on_kick`, and `_on_adjust_blinds` each guard only with `if not self.is_host`. There is no check that the message came from the host's `conn_id`. Any connected peer can send `{"type": "pause"}`, `{"type": "kick", "payload": {...}}`, or `{"type": "adjust_blinds", "payload": {"bb": 1}}` to all participants and have it take effect immediately.  
**Fix:** At the top of each handler, add `if conn_id != self._host_conn_id: return`. The `conn_id` argument is already available; thread it into `_on_pause`, `_on_resume`, and `_on_adjust_blinds` the same way it is for `_on_kick`.

---

### C-3 · CRITICAL — No message-size limit (OOM / DoS)
**File:** `holdem/p2p/transport.py` lines 121–125  
```python
header = await reader.readexactly(4)
length = struct.unpack(">I", header)[0]   # up to 4 GB
body   = await reader.readexactly(length)
```
A peer sends `\xff\xff\xff\xff` as the length header, causing the server to `await reader.readexactly(4_294_967_295)`. On most systems this exhausts memory and crashes the process. There is no cap and no timeout.  
**Fix:** Add `MAX_MSG = 1 << 20  # 1 MB` and check `if length > MAX_MSG: raise ValueError(f"oversized frame: {length}")` immediately after unpacking the length.

---

### H-6 · HIGH — No timestamp validation; replay attacks are undetected
**File:** `holdem/p2p/wire.py`  
`wire.pack()` embeds a `ts` (Unix ms) field in every envelope, but `wire.unpack()` never checks it. An attacker who captures a valid signed message can replay it indefinitely without detection.  
**Fix:** In `wire.unpack()`, after verifying the signature, assert `abs(time.time()*1000 - msg["ts"]) < 30_000` (30-second window) and raise if outside it.

---

### H-7 · HIGH — `wire.unpack` accepts a hash field it never verifies
**File:** `holdem/p2p/wire.py` lines 62–73  
The function pops `hash` and `sig`, verifies the signature, then puts them back. The `hash` field (SHA-256 of the full signed envelope, used for hash-chaining) is trusted verbatim without recomputing it. The `prev` chain field is also never compared against the actual previous hash. Chain-linkage is declared in the design but never enforced, making message replay and selective drop undetectable.  
**Fix:** After the signature check, recompute `expected_hash = hashlib.sha256(canonical + sig_bytes).hexdigest()` and verify `msg["hash"] == expected_hash`. Then maintain a per-peer `last_hash` and check `msg["prev"] == last_hash`.

---

### M-7 · MEDIUM — `peer_id_prefix` from room code never verified
**File:** `holdem/onboarding.py` (join flow), `holdem/p2p/invite.py` line 31  
The invite room code encodes the host's first 8 bytes of public key as `peer_id_prefix`. The join flow reads this field from `parse_room_code()` but never checks that the responding host's `pubkey` starts with those bytes. A man-in-the-middle who learns the room code can impersonate the host.  
**Fix:** After receiving the host's `player_ack`, compare `bytes.fromhex(host_pubkey)[:8]` against the `peer_id_prefix` from the decoded room code and close the connection if they differ.

---

### M-8 · MEDIUM — Any TCP connection can join a session without authentication
**File:** `holdem/p2p/session.py` lines 111–129  
`_on_player_info` adds any peer that sends a `player_info` message to the session roster. There is no check that the peer knows the rendezvous key or presents a pubkey matching the room code.  
**Fix:** Include the rendezvous key (or a HMAC of it) as a credential in `player_info`. Host rejects peers that send the wrong credential.

---

### M-10 · MEDIUM — Private key stored as plaintext JSON
**File:** `holdem/p2p/identity.py` lines 36–40  
The Ed25519 private key is base64-encoded and written as plain JSON to `~/.texas_holdem_identity.json`. Any process or user with filesystem read access can extract the key and forge signatures for that identity.  
**Fix:** Encrypt the private key at rest using a key derived from a machine-specific secret (e.g., Windows DPAPI / macOS Keychain), or at minimum use `os.chmod(path, 0o600)` immediately after writing.

---

### L-1 · LOW — Room-code version byte is parsed but never validated
**File:** `holdem/p2p/invite.py` line 31  
`parse_room_code()` extracts the `version` byte and returns it, but no caller checks it. A client running a newer protocol version will silently accept a code from an incompatible older version.  
**Fix:** `if version != CURRENT_VERSION: raise ValueError(f"unsupported room code version {version}")`.

---

## 2. P2P / Networking

### H-1 · HIGH — Host announces `0.0.0.0` as its LAN address
**File:** `holdem/p2p/transport.py` line 198  
```python
return f"0.0.0.0:{actual_port}"
```
`_start_server` binds on `0.0.0.0` (correct for accepting connections) but returns that literal string as the announced address. The `announce()` function broadcasts it via multicast. When a joiner calls `transport.connect("0.0.0.0:PORT")`, the OS rejects the connection because `0.0.0.0` is not a routable destination. Multiplayer over LAN is entirely broken.  
**Fix:** Detect the host's LAN IP before returning: use `socket.gethostbyname(socket.gethostname())` or iterate `socket.getaddrinfo(socket.gethostname(), None)` to find the first non-loopback IPv4 address, then return `f"{lan_ip}:{actual_port}"`.

---

### H-2 · HIGH — `_ensure_loop()` has a race condition; multiple event loops can be created
**File:** `holdem/p2p/transport.py` lines 341–353  
The check-then-set pattern for `_loop` is not atomic. Two threads calling `_ensure_loop()` simultaneously can both pass the `if _loop is not None` guard and each create a new `asyncio.new_event_loop()`. The second loop overwrites the first; all coroutines scheduled on the first loop are orphaned and never complete.  
**Fix:** Add a module-level `_loop_lock = threading.Lock()` and hold it for the entire body of `_ensure_loop()`.

---

### H-3 · HIGH — `_announce_task` is a `concurrent.futures.Future`, not an `asyncio.Task`
**File:** `holdem/p2p/transport.py` lines 299–301, 261–263  
`asyncio.run_coroutine_threadsafe()` returns a `concurrent.futures.Future`. The module types it as `asyncio.Task`. In `stop()`, it calls `_loop.call_soon_threadsafe(_announce_task.cancel)`. `concurrent.futures.Future.cancel()` can only cancel if the coroutine hasn't started; once the announce loop is running, `cancel()` silently returns `False` and the loop keeps announcing forever.  
**Fix:** Schedule the coroutine from inside the asyncio thread so it returns a real `asyncio.Task`:
```python
async def _schedule_announce(coro):
    return asyncio.ensure_future(coro)
fut = asyncio.run_coroutine_threadsafe(_schedule_announce(_loop_announce()), _loop)
_announce_task = fut.result(timeout=5)   # now a real asyncio.Task
```
Then `stop()` can call `_loop.call_soon_threadsafe(_announce_task.cancel)` correctly.

---

### H-4 · HIGH — `handle_disconnect` fires a UI callback from the transport thread
**File:** `holdem/p2p/session.py` lines 210–211  
```python
if self.on_player_list_changed:
    self.on_player_list_changed(list(self.players.values()))
```
This executes in the asyncio event-loop thread (the transport's background thread). If the registered callback touches any Tkinter widget directly, this is a thread-safety violation that can cause silent state corruption or an outright crash.  
**Fix:** The docstring on line 60 already says callers should use `root.after(0, ...)`. Enforce it: make `Session` accept a `marshal` callable (defaulting to a no-op passthrough) and route every `on_player_list_changed` call through `marshal(callback, args)`.

---

### H-5 · HIGH — `handle_game_action` seat validation can be bypassed with an out-of-range seat index
**File:** `holdem/p2p/session.py` lines 373–376  
```python
if 0 <= seat < len(self._seat_order):
    if self._seat_order[seat] != conn_id:
        return
```
If `seat < 0` or `seat >= len(self._seat_order)`, the outer `if` is False and the ownership check is **skipped entirely**. The action then reaches `engine.act()` attributed to whatever `engine.actor` happens to be. A peer can send `{"seat": -1, "action": "fold", "amount": 0}` to force the acting player to fold.  
**Fix:** Invert the guard: `if not (0 <= seat < len(self._seat_order)) or self._seat_order[seat] != conn_id: return`.

---

### H-10 · HIGH — Multiplayer game ignores the betting structure and run-it-twice setting
**File:** `holdem/gui.py` `_mp_new_game()` (search `_mp_new_game`)  
The host's `Engine` is created as `Engine(players, sb=sb, bb=bb)` with no `structure` argument. The engine defaults to `"No-Limit"`. If the host chose `"Pot-Limit"` or `"Fixed-Limit"` in the lobby settings, all peers play with the wrong betting structure. Similarly, `rit` (run-it-twice) and `straddles` flags from `table_settings` are not forwarded to the engine.  
**Fix:** Extract `structure`, `rit`, and `straddles` from `table_settings` and pass them to the `Engine` constructor.

---

### H-11 · HIGH — Host migration leaves the new host without a working engine
**File:** `holdem/gui.py` `_mp_on_host_changed()`, `holdem/p2p/session.py` lines 213–230  
When the original host disconnects mid-hand, `_elect_new_host()` promotes the lowest-join-order peer to host. That peer's `gui.py` calls `_mp_broadcast_state()`, but the new host's engine was a stub that received state from `game_state` broadcasts — it does not have the deck, the burn cards, or the hand state needed to continue dealing or settling side pots. The game silently freezes or crashes at the next deal.  
**Fix:** The full engine state (deck order, seeded RNG, burn cards) must be transferred to the new host before promoting it. The simplest approach: at hand start, the old host sends an encrypted snapshot of the deck seed to all peers; on host migration, the new host reconstructs the engine from that snapshot.

---

### H-12 · HIGH — `on_connect` callback assigns a remote peer's `conn_id` as the host's local player
**File:** `holdem/onboarding.py` `_create_game_dialog()` (search `add_local_player`)  
```python
_transport.on_connect(lambda cid, addr:
    sess.add_local_player(cid) if not sess.players else None)
```
The first `on_connect` fires when the **first remote peer** connects, not when the host itself connects. The host's own identity is registered under that peer's `conn_id`. Consequently `sess.local_conn_id` is wrong, the host appears in the player list with the wrong identity, and `_elect_new_host()` will never correctly identify the host peer.  
**Fix:** The host should add itself to the session at dialog-open time with a stable local ID (e.g., from `identity.py`'s public key prefix), not inside an `on_connect` callback.

---

### M-1 · MEDIUM — `json.JSONDecodeError` from a malformed peer frame is uncaught
**File:** `holdem/p2p/transport.py` line 125  
`json.loads(body)` can raise `json.JSONDecodeError` if a peer sends non-JSON bytes. This exception propagates out of `_read_msg()` and is not caught by the `except (asyncio.IncompleteReadError, ConnectionResetError, EOFError)` block in `_handle_connection` (line 156). The connection handler task crashes silently and the peer is never added to the disconnect list, leaving a zombie entry in `_writers`.  
**Fix:** Catch `json.JSONDecodeError` in `_read_msg` (log and re-raise as a custom `ProtocolError`) or extend the except tuple in `_handle_connection`.

---

### M-2 · MEDIUM — `broadcast()` fires N independent coroutines with no ordering guarantee
**File:** `holdem/p2p/transport.py` lines 239–245  
Each `_send_to` coroutine is submitted via separate `run_coroutine_threadsafe` calls. The asyncio scheduler may interleave them with other pending I/O. Two rapid `broadcast()` calls (e.g., `broadcast_game_state()` followed by `deal_private`) can arrive in reverse order at a slow peer.  
**Fix:** Use a per-connection `asyncio.Queue` instead of direct writes. Each connection has exactly one writer coroutine that drains its queue; this gives strict FIFO ordering per peer.

---

### M-3 · MEDIUM — `send()` silently discards the returned `Future`
**File:** `holdem/p2p/transport.py` line 223  
```python
asyncio.run_coroutine_threadsafe(_send_to(conn_id, msg), _loop)
```
The returned `concurrent.futures.Future` is thrown away. If `_send_to` raises (e.g., `BrokenPipeError`), the exception is silently lost. The caller never knows the message was not delivered.  
**Fix:** Either store the future and check it (`.result()` with a short timeout) or add an exception handler inside `_send_to` that emits a log warning and optionally calls the `on_disconnect` callbacks for that peer.

---

### M-11 · MEDIUM — `_hash_chain` is initialised but never read or updated
**File:** `holdem/p2p/session.py` line 51  
`self._hash_chain = "0" * 64` is set in `__init__` and never touched again. Wire messages include a `prev` hash-chain field that is supposed to chain messages cryptographically, but the session never updates or validates it. The chain linkage is vestigial.  
**Fix:** Either implement the chain (update `_hash_chain` after each outbound message, verify `msg["prev"]` on each inbound message) or remove the field to avoid misleading reviewers.

---

### L-5 · LOW — `asyncio.ensure_future()` is deprecated
**File:** `holdem/p2p/transport.py` lines 190, 197, 214  
`asyncio.ensure_future()` was soft-deprecated in Python 3.10 in favour of `asyncio.create_task()`. This generates deprecation warnings on Python 3.12+.  
**Fix:** Replace all three call sites with `asyncio.create_task(...)`.

---

## 3. Correctness (Poker Engine)

### H-8 · HIGH — EV cashout bypasses `hand_logger.on_settle()`
**File:** `holdem/gui.py` `_maybe_offer_cashout()` (search `_maybe_offer_cashout`)  
When the user accepts an EV cashout, the routine directly mutates `p.stack`, `p.won`, and `p.total_live` and calls `self._finish_hand()` without ever calling `self.hand_logger.on_settle()`. The hand is not recorded in the JSONL history. Additionally, the cashout computes equity synchronously on the main thread with `equity(..., sims=800)`, blocking Tkinter for roughly 100–200 ms.  
**Fix:** Build a synthetic settle result dict in the cashout path and pass it to `hand_logger.on_settle()`. Move the equity computation to a background thread (same pattern as `start_equity()`) and show a brief spinner.

---

### M-5 · MEDIUM — `_on_player_list` never updates an existing player's `ready` flag
**File:** `holdem/p2p/session.py` lines 131–154  
```python
if cid and cid not in self.players:
    self.players[cid] = Player(...)   # only creates; never updates
```
When the host broadcasts an updated player list (e.g., after a peer toggles "Ready"), existing `Player` objects on non-host peers are not updated. The lobby UI never reflects ready-status changes for peers who joined before the latest broadcast.  
**Fix:** Change the `if cid not in` branch to an `if/else`: if the player exists, update `p.ready`, `p.nickname`, and `p.avatar_b64` in-place.

---

### M-6 · MEDIUM — `_elect_new_host` promotes any peer whose `local_conn_id` is empty
**File:** `holdem/p2p/session.py` lines 218–219  
```python
am_new_host = (new_host_conn == self.local_conn_id
               or self.local_conn_id == "")
```
`local_conn_id` starts as `""` and is only set when the host sends a `player_ack`. If a peer never received a `player_ack` (e.g., it joined and the host immediately dropped), `local_conn_id` remains `""` and **every such peer** claims the host role simultaneously, producing a split-brain.  
**Fix:** Remove the `or self.local_conn_id == ""` branch. A peer that never received a `player_ack` cannot reliably identify itself in the join-order list and should not become host.

---

### M-9 · MEDIUM — Action `type` string is not validated before reaching `engine.act()`
**File:** `holdem/p2p/session.py` line 370, `holdem/gui.py` `_mp_peer_acted()`  
`payload.get("action", "fold")` is forwarded verbatim to `engine.act(seat, action, amount)`. The engine's `act()` method dispatches on exact string values `"fold"`, `"call"`, `"raise"`. An unexpected string (e.g., `"check"`, `""`, `"raise_allin"`) falls through all branches silently, leaving `engine.actor` unchanged and the hand stuck.  
**Fix:** In `handle_game_action`, validate: `if action not in ("fold", "call", "raise", "check"): return`.

---

### M-13 · MEDIUM — `_mp_new_game()` doesn't forward run-it-twice or straddle flags to the engine
**File:** `holdem/gui.py` `_mp_new_game()`  
This partially overlaps H-10. Even within No-Limit, the `rit` ("Ask"/"Always"/"Never") and `straddles` (bool) table rules are extracted from `table_settings` in the join flow but never set on the engine or stored for later reference. The host may prompt for run-it-twice when the table rule is "Never".  
**Fix:** Store the full `table_settings` dict on `self` at game-start and consult it in `_maybe_ask_rit()` and the straddle-arm handler.

---

### L-3 · LOW — `engine.pot` is O(n) and called on every UI repaint
**File:** `holdem/engine.py` lines 344–346  
```python
@property
def pot(self):
    return sum(p.total for p in self.players)
```
`pot` is called in `legal()`, `settle()`, `broadcast_game_state()`, and several GUI repaint loops. At 9 players this is negligible, but caching it as a running total (updated in `act()` and `_post()`) would be cleaner and eliminate the recomputation.  
**Fix:** Maintain `self._pot: int = 0` and update it whenever chips move. Keep the property as a fallback assertion in debug builds.

---

## 4. GUI / Crash Risks

### H-9 · HIGH — `pyproject.toml` does not declare `holdem.p2p` as a package
**File:** `pyproject.toml` line 29  
```toml
[tool.setuptools]
packages = ["holdem"]
```
Setuptools does not auto-discover sub-packages unless `find:` or `find_namespace:` is used. When a user does `pip install .`, the `holdem/p2p/` directory is silently omitted. Any import of `holdem.p2p.*` raises `ModuleNotFoundError` at runtime.  
**Fix:** Either switch to auto-discovery:
```toml
[tool.setuptools.packages.find]
where = ["."]
```
or list the subpackage explicitly:
```toml
packages = ["holdem", "holdem.p2p"]
```

---

### M-4 · MEDIUM — `HERO = 0` is hardcoded throughout and used in multiplayer mode
**File:** `holdem/gui.py` line 87 (and ~15 downstream call sites)  
`HERO = 0` is a module-level constant. In single-player it is always correct. In multiplayer, the local seat is `self._mp_local_seat`, which can be any value 0–8. Several repaint functions (e.g., `loop()`, `start_equity()`, `showdown()`) still reference `HERO` instead of `self._mp_local_seat`, causing wrong seat highlighting, incorrect equity display, and potentially incorrect card display.  
**Fix:** Add a `@property hero_seat` on the `App` class that returns `self._mp_local_seat if self._mp_session else HERO` and replace the raw `HERO` constant at each call site with `self.hero_seat`.

---

### M-12 · MEDIUM — AI emote selection uses the unseeded module-level `random`
**File:** `holdem/gui.py` `ai_turn()` (search `random.random() < 0.20`)  
```python
if random.random() < 0.20:   # module-level random, not self.rng
```
All other engine-side randomness flows through `self.rng` (a seeded `random.Random` instance) to allow reproducible replays. This one call diverges from the seeded path, breaking determinism.  
**Fix:** Change to `if self.rng.random() < 0.20:`.

---

### L-2 · LOW — `hand_history._persist()` silently swallows all exceptions
**File:** `holdem/hand_history.py` (search `except Exception: pass`)  
If the history file is read-only, the disk is full, or JSON serialisation fails, the exception is caught and discarded without any logging. The user loses hand history silently.  
**Fix:** At minimum, `log.warning("hand_history persist failed: %s", e)` in the except block.

---

### L-4 · LOW — `identity.py` ignores `HOLDEM_CONFIG_DIR`
**File:** `holdem/p2p/identity.py` lines 20–25  
`settings.py` respects the `HOLDEM_CONFIG_DIR` environment variable for all other persistent files. `identity.py` hard-codes `~/.texas_holdem_identity.json`, making it impossible to relocate the identity file via the same env var that controls everything else.  
**Fix:** Import `config_dir()` from `settings` and write to `config_dir() / "identity.json"` instead.

---

## 5. Code Quality

### M-14 · MEDIUM — `settings.get()` and `settings.set()` read the entire JSON file on every call
**File:** `holdem/settings.py` lines 238–253  
Both functions call `load()`, which `read_text()` + `json.loads()` the config file each time. In the hot path this is called after every hand (XP update, `hands_played_total`, bankroll, daily bonus check) — up to 5 separate file reads per hand. On slow filesystems or antivirus-intercepted paths this adds measurable latency.  
**Fix:** Add an in-memory `_cache: dict | None = None` at module level. `load()` populates it on first call; `save()` invalidates it. `get()` and `set()` go through the cache.

---

### L-6 · LOW — Duplicate card serialisation logic in `session.py` and `gui.py`
**File:** `holdem/p2p/session.py` lines 325–332, `holdem/gui.py` (search `_card_str`)  
`Session._card_to_str()` and `gui.py`'s local `_card_str()` both convert a `Card(v, s)` to a string using different lookup tables. If the format ever changes, both must be updated in sync.  
**Fix:** Move the canonical card serialiser to `holdem/engine.py` (or a new `holdem/cards.py`) as `card_to_str(card) -> str` and import it in both places.

---

*End of audit.*
