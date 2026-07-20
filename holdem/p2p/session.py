"""
Multiplayer session state machine.

States
------
IDLE    -> no game in progress
LOBBY   -> connected, waiting for players / host to start
PLAYING -> hand(s) in progress
ENDED   -> game over

Lobby protocol
--------------
peer  -> host : {"type": "player_info",  "payload": {"nickname": ..., "avatar_b64": ...}}
host  -> all  : {"type": "player_list",  "payload": {"players": [...]}}
host  -> all  : {"type": "game_start",   "payload": {"table_settings": {...}, "seat_order": [...]}}

In-game (Phase 1 -- on top of transport)
-----------------------------------------
any -> all : {"type": "action",  "action": "fold"|"call"|"raise", "amount": N}
host-> all : {"type": "deal",    ...}

Verifiable shuffle (Phase 2)
-----------------------------
host -> all  : {"type": "shuffle_start",          "payload": {"commit_hex": ..., "x25519_pubkey_hex": ...}}
peer -> host : {"type": "shuffle_commit",         "payload": {"commit_hex": ..., "x25519_pubkey_hex": ...}}
host -> all  : {"type": "shuffle_commit_collect", "payload": {"commits": {conn_id: hex, ...}}}
peer -> host : {"type": "shuffle_reveal",         "payload": {"seed_hex": ..., "nonce_hex": ...}}
host -> all  : {"type": "shuffle_reveal_collect", "payload": {"reveals": {conn_id: {seed_hex, nonce_hex}, ...}}}
host -> peer : {"type": "shuffle_deal",           "payload": {"seat": N, "encrypted_hex": "..."}}
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional

_log = logging.getLogger(__name__)


@dataclass
class Player:
    conn_id:           str
    peer_id:           str
    nickname:          str
    avatar_b64:        str
    is_host:           bool  = False
    ready:             bool  = False
    seat_index:        int   = -1
    # X25519 pubkey for hole-card encryption (populated from player_info or shuffle_commit)
    x25519_pubkey_hex: str   = ""


class Session:
    """Tracks lobby membership and drives the LOBBY -> PLAYING transition."""

    def __init__(self, is_host: bool, nickname: str, avatar_b64: str,
                 transport=None):
        self.is_host    = is_host
        # transport module (or a mock) providing broadcast()/send().
        # Defaults to the real global transport; tests inject an
        # in-memory one so N sessions can run in one process.
        if transport is None:
            from holdem.p2p import transport as _t_module
            transport = _t_module
        self._transport = transport
        self.state      = "LOBBY"
        # conn_id -> Player (includes local player once we have a conn_id)
        self.players:   dict[str, Player] = {}
        self.local_nickname  = nickname
        self.local_avatar    = avatar_b64
        self._lock           = threading.Lock()

        # M-11: per-peer hash-chain tracking (conn_id -> last seen hash)
        self._peer_last_hash: dict[str, str] = {}

        # Join order & host tracking
        self._join_order: list[str] = []       # conn_ids in join order (host-side IDs)
        self.local_conn_id: str = ""           # this peer's own conn_id as seen by host
        self._host_conn_id: str = ""           # conn_id used to reach the host (peers only)

        # H-11: last received game_state payload (used for host migration)
        self._last_game_state: dict = {}
        # Last table settings (used by _mp_new_game in gui.py)
        self._last_table_settings: dict = {}

        # Verifiable shuffle state
        self._shuffle_round = None             # holdem.p2p.shuffle.ShuffleRound | None

        # UI callbacks -- set by the lobby after constructing the session.
        # Both are called from the transport's background thread; callers
        # should route back to the Tk main thread via root.after(0, ...).
        self.on_player_list_changed: Optional[Callable[[List[Player]], None]] = None
        self.on_game_start:          Optional[Callable[[dict], None]]         = None
        self.on_game_state:          Optional[Callable[[dict], None]]         = None
        self.on_deal_private:        Optional[Callable[[dict], None]]         = None
        self.on_chat:                Optional[Callable[[str, str], None]]     = None
        self.on_action:              Optional[Callable[[int, str, int], None]]= None
        self.on_host_changed:        Optional[Callable[[bool], None]]         = None
        self.on_pause:               Optional[Callable[[], None]]             = None
        self.on_resume:              Optional[Callable[[], None]]             = None
        self.on_kick:                Optional[Callable[[dict], None]]         = None
        self.on_adjust_blinds:       Optional[Callable[[dict], None]]         = None

        # Shuffle callbacks
        # on_shuffle_ready(deck_indices)  -- host: all reveals verified, deck ready
        self.on_shuffle_ready: Optional[Callable[[list], None]]  = None
        # on_shuffle_deal(payload_dict)   -- peer: encrypted hole cards arrived
        self.on_shuffle_deal: Optional[Callable[[dict], None]]   = None
        # on_shuffle_cheat(conn_id)       -- peer: a reveal failed verification
        self.on_shuffle_cheat: Optional[Callable[[str], None]]   = None

        # Engine ref (host only) and seat order
        self._engine     = None
        self._seat_order: list[str] = []

    # ------------------------------------------------------------------
    # Message dispatch (called by transport on_message handler)
    # ------------------------------------------------------------------

    def handle_message(self, conn_id: str, msg: dict) -> None:
        """Route an incoming transport message to the appropriate handler."""
        # M-11 / H-3: per-message integrity is enforced at the transport
        # layer (C-1: every envelope is signature-verified in wire.unpack).
        # The hash *chain* linking successive messages is not yet threaded —
        # senders still emit prev="0"*64 (see wire.pack). We record each
        # message hash so the chain can be verified once per-peer sequencing
        # is implemented (docs/MULTIPLAYER.md Phase 1), but we do NOT drop on
        # a prev mismatch here: doing so would reject every message after the
        # first, since prev is not populated. Detect a *real* chain (prev set
        # to something other than genesis) and enforce it only then.
        if "hash" in msg and "prev" in msg:
            last = self._peer_last_hash.get(conn_id)
            if msg["prev"] != "0" * 64 and last is not None and msg["prev"] != last:
                _log.warning(
                    "session: hash-chain broken for %s "
                    "(expected prev=%s, got %s) — dropping",
                    conn_id, last[:16], msg["prev"][:16]
                )
                return
            self._peer_last_hash[conn_id] = msg["hash"]

        t = msg.get("type")
        if t == "player_info":
            self._on_player_info(conn_id, msg)
        elif t == "player_list":
            self._on_player_list(conn_id, msg)
        elif t == "player_ack":
            self._on_player_ack(conn_id, msg)
        elif t == "game_start":
            self._on_game_start(msg)
        elif t == "ready":
            self._on_ready(conn_id, msg)
        elif t == "action":
            self.handle_game_action(conn_id, msg)
        elif t == "game_state":
            self._on_game_state(msg)
        elif t == "deal_private":
            self._on_deal_private(msg)
        elif t == "chat":
            self._on_chat(conn_id, msg)
        elif t == "pause":
            self._on_pause(conn_id, msg)
        elif t == "resume":
            self._on_resume(conn_id, msg)
        elif t == "kick":
            self._on_kick(conn_id, msg)
        elif t == "adjust_blinds":
            self._on_adjust_blinds(conn_id, msg)
        # --- verifiable shuffle ---
        elif t == "shuffle_start":
            self._on_shuffle_start(conn_id, msg)
        elif t == "shuffle_commit":
            self._on_shuffle_commit(conn_id, msg)
        elif t == "shuffle_commit_collect":
            self._on_shuffle_commit_collect(conn_id, msg)
        elif t == "shuffle_reveal":
            self._on_shuffle_reveal(conn_id, msg)
        elif t == "shuffle_reveal_collect":
            self._on_shuffle_reveal_collect(conn_id, msg)
        elif t == "shuffle_deal":
            self._on_shuffle_deal(msg)

    def _on_player_info(self, conn_id: str, msg: dict) -> None:
        """Host receives identity from a newly connected peer."""
        payload = msg.get("payload", {})
        with self._lock:
            self.players[conn_id] = Player(
                conn_id           = conn_id,
                peer_id           = msg.get("pubkey", "")[:16],
                nickname          = payload.get("nickname",           "Player"),
                avatar_b64        = payload.get("avatar_b64",         ""),
                x25519_pubkey_hex = payload.get("x25519_pubkey_hex",  ""),
                is_host           = False,
            )
            if conn_id not in self._join_order:
                self._join_order.append(conn_id)
        if self.is_host:
            # Tell the peer their host-side conn_id so they can self-identify
            self._transport.send(conn_id, {"type": "player_ack",
                               "payload": {"your_conn_id": conn_id}})
            self._broadcast_player_list()

    def _on_player_list(self, conn_id: str, msg: dict) -> None:
        """Non-host receives updated player list from the host."""
        payload = msg.get("payload", {})
        players_data = payload.get("players", [])
        with self._lock:
            for p in players_data:
                cid = p.get("conn_id", "")
                if not cid:
                    continue
                if cid not in self.players:
                    self.players[cid] = Player(
                        conn_id           = cid,
                        peer_id           = p.get("peer_id",           ""),
                        nickname          = p.get("nickname",          "Player"),
                        avatar_b64        = p.get("avatar_b64",        ""),
                        x25519_pubkey_hex = p.get("x25519_pubkey_hex", ""),
                        is_host           = p.get("is_host",           False),
                        ready             = p.get("ready",             False),
                    )
                else:
                    # M-5: update mutable fields on existing Player objects
                    existing = self.players[cid]
                    existing.ready             = p.get("ready",             existing.ready)
                    existing.nickname          = p.get("nickname",          existing.nickname)
                    existing.avatar_b64        = p.get("avatar_b64",        existing.avatar_b64)
                    existing.is_host           = p.get("is_host",           existing.is_host)
                    existing.x25519_pubkey_hex = p.get("x25519_pubkey_hex", existing.x25519_pubkey_hex)
            # Mirror join order from the host's authoritative list (non-hosts only)
            self._join_order = [
                p.get("conn_id", "") for p in players_data
                if p.get("conn_id", "") and not p.get("is_host", False)
            ]
            snapshot = list(self.players.values())
        if self.on_player_list_changed:
            self.on_player_list_changed(snapshot)

    def _on_player_ack(self, conn_id: str, msg: dict) -> None:
        """Peer receives its own host-side conn_id from the host."""
        payload = msg.get("payload", {})
        self.local_conn_id = payload.get("your_conn_id", "")
        self._host_conn_id = conn_id   # conn_id of the connection to the host

    def _on_game_start(self, msg: dict) -> None:
        self.state = "PLAYING"
        payload = msg.get("payload", {})
        self._seat_order = payload.get("seat_order", [])
        # Store table settings so _mp_new_game in gui.py can read them
        ts = payload.get("table_settings", {})
        if ts:
            self._last_table_settings = ts
        if self.on_game_start:
            self.on_game_start(payload)

    def _on_ready(self, conn_id: str, msg: dict) -> None:
        payload = msg.get("payload", {})
        self.set_ready(conn_id, payload.get("ready", False))

    def _on_game_state(self, msg: dict) -> None:
        payload = msg.get("payload", {})
        # H-11: keep the most recent game state for use by host-migration engine rebuild
        self._last_game_state = payload
        if self.on_game_state:
            self.on_game_state(payload)

    def _on_deal_private(self, msg: dict) -> None:
        if self.on_deal_private:
            self.on_deal_private(msg.get("payload", {}))

    def _on_chat(self, conn_id: str, msg: dict) -> None:
        payload = msg.get("payload", {})
        nickname = payload.get("nickname", "Player")
        text = payload.get("text", "")
        if self.on_chat:
            self.on_chat(nickname, text)
        if self.is_host:
            # Re-broadcast to all peers (echo back to sender too)
            self._transport.broadcast(msg)

    # ------------------------------------------------------------------
    # Verifiable shuffle — protocol message handlers
    # ------------------------------------------------------------------

    def start_shuffle(self) -> None:
        """Host: begin a new shuffle round for the upcoming hand.

        Generates the host's own commit and broadcasts ``shuffle_start`` to
        all peers.  Returns immediately; the shuffle completes asynchronously
        when all peers have revealed (``on_shuffle_ready`` callback).
        """
        if not self.is_host:
            raise RuntimeError("Only the host can call start_shuffle()")

        from holdem.p2p.shuffle import ShuffleRound
        from holdem.p2p import identity as _id

        all_ids = list(self._seat_order) if self._seat_order else list(self.players)
        if not self.local_conn_id:
            self.local_conn_id = next(
                (p.conn_id for p in self.players.values() if p.is_host), "")

        self._shuffle_round = ShuffleRound(
            local_conn_id=self.local_conn_id,
            all_conn_ids=all_ids,
        )
        sr = self._shuffle_round

        # Generate host's own commit and record local X25519 pubkey
        commit = sr.local_commit()
        host_x25519_pub = _id.x25519_public_key_bytes().hex()
        sr.record_x25519_pubkey(self.local_conn_id, _id.x25519_public_key_bytes())

        self._transport.broadcast({
            "type": "shuffle_start",
            "payload": {
                "commit_hex":       commit.hex(),
                "x25519_pubkey_hex": host_x25519_pub,
            },
        })
        _log.debug("shuffle: host broadcasted commit and waiting for peer commits")

    def _on_shuffle_start(self, conn_id: str, msg: dict) -> None:
        """Peer: host has started a shuffle round — generate and send our commit."""
        if self.is_host:
            return   # host sent this, not a receiver
        if conn_id != self._host_conn_id and self._host_conn_id:
            _log.warning("shuffle_start from non-host %s — ignoring", conn_id)
            return

        from holdem.p2p.shuffle import ShuffleRound
        from holdem.p2p import identity as _id

        payload = msg.get("payload", {})
        host_commit_hex     = payload.get("commit_hex", "")
        host_x25519_pub_hex = payload.get("x25519_pubkey_hex", "")

        all_ids = list(self._seat_order) if self._seat_order else list(self.players)
        self._shuffle_round = ShuffleRound(
            local_conn_id=self.local_conn_id,
            all_conn_ids=all_ids,
        )
        sr = self._shuffle_round

        # Record host's commit
        if host_commit_hex:
            sr.record_commit(conn_id, bytes.fromhex(host_commit_hex))
        if host_x25519_pub_hex:
            sr.record_x25519_pubkey(conn_id, bytes.fromhex(host_x25519_pub_hex))

        # Generate our own commit and send to host
        my_commit   = sr.local_commit()
        my_x25519   = _id.x25519_public_key_bytes().hex()
        sr.record_x25519_pubkey(self.local_conn_id, _id.x25519_public_key_bytes())

        self._transport.send(self._host_conn_id, {
            "type": "shuffle_commit",
            "payload": {
                "commit_hex":        my_commit.hex(),
                "x25519_pubkey_hex": my_x25519,
            },
        })
        _log.debug("shuffle: peer sent commit to host")

    def _on_shuffle_commit(self, conn_id: str, msg: dict) -> None:
        """Host: receive a commit from a peer."""
        if not self.is_host or self._shuffle_round is None:
            return

        payload = msg.get("payload", {})
        commit_hex     = payload.get("commit_hex", "")
        x25519_pub_hex = payload.get("x25519_pubkey_hex", "")

        sr = self._shuffle_round
        if commit_hex:
            sr.record_commit(conn_id, bytes.fromhex(commit_hex))
        if x25519_pub_hex:
            sr.record_x25519_pubkey(conn_id, bytes.fromhex(x25519_pub_hex))

        _log.debug("shuffle: host got commit from %s (have %d/%d)",
                   conn_id, len(sr._commits), len(sr.all_conn_ids))

        if sr.all_commits_received():
            self._host_broadcast_commit_collect()

    def _host_broadcast_commit_collect(self) -> None:
        """Host: broadcast all commits so every peer can verify theirs is included."""

        sr = self._shuffle_round
        commits_payload = {cid: commit.hex() for cid, commit in sr._commits.items()}

        self._transport.broadcast({
            "type": "shuffle_commit_collect",
            "payload": {"commits": commits_payload},
        })
        _log.debug("shuffle: host broadcasted commit_collect")

    def _on_shuffle_commit_collect(self, conn_id: str, msg: dict) -> None:
        """Peer: host has collected all commits — send our reveal."""
        if self.is_host or self._shuffle_round is None:
            return
        if conn_id != self._host_conn_id and self._host_conn_id:
            return


        sr = self._shuffle_round
        payload = msg.get("payload", {})
        commits = payload.get("commits", {})

        # Verify our commit is in the collection
        if self.local_conn_id and self.local_conn_id not in commits:
            _log.warning("shuffle: our commit not in commit_collect — aborting")
            return

        self._transport.send(self._host_conn_id, {
            "type": "shuffle_reveal",
            "payload": {
                "seed_hex":  sr.local_seed_hex,
                "nonce_hex": sr.local_nonce_hex,
            },
        })
        _log.debug("shuffle: peer sent reveal to host")

    def _on_shuffle_reveal(self, conn_id: str, msg: dict) -> None:
        """Host: verify a reveal from a peer."""
        if not self.is_host or self._shuffle_round is None:
            return

        payload  = msg.get("payload", {})
        seed_hex  = payload.get("seed_hex",  "")
        nonce_hex = payload.get("nonce_hex", "")

        sr = self._shuffle_round
        try:
            sr.record_reveal(conn_id,
                             bytes.fromhex(seed_hex),
                             bytes.fromhex(nonce_hex))
        except (ValueError, Exception) as exc:
            _log.error("shuffle: reveal verification FAILED for %s: %s", conn_id, exc)
            return

        _log.debug("shuffle: host verified reveal from %s (%d/%d)",
                   conn_id, len(sr._seeds), len(sr.all_conn_ids))

        if sr.all_reveals_received():
            self._host_finalise_shuffle()

    def _host_finalise_shuffle(self) -> None:
        """Host: all reveals verified — compute deck, encrypt, and deal."""

        sr = self._shuffle_round
        deck_indices = sr.shuffled_deck()

        # Broadcast reveals so every peer can independently verify the deck.
        # H-1: reveals_snapshot() emits the real per-seat nonce (not the
        # commit), so any peer recomputing SHA256(seed||nonce) matches the
        # commitment and can reproduce the deck.
        self._transport.broadcast({
            "type": "shuffle_reveal_collect",
            "payload": {"reveals": sr.reveals_snapshot()},
        })
        _log.debug("shuffle: host broadcasted reveal_collect, deck derived")

        # Notify the host's own GUI that the deck is ready
        if self.on_shuffle_ready:
            self.on_shuffle_ready(deck_indices)

    def _on_shuffle_reveal_collect(self, conn_id: str, msg: dict) -> None:
        """Peer: verify every revealed seed against its commitment (H-1).

        Each peer already holds the commits from the commit_collect phase.
        Recompute SHA256(seed||nonce) for every seat and confirm it matches
        the committed value; if any seat fails, the host cheated or a message
        was tampered with, and the hand must not proceed.
        """
        from holdem.p2p.shuffle import verify_commit, derive_master_seed, \
            deterministic_shuffle

        sr = self._shuffle_round
        if sr is None:
            return
        reveals = msg.get("payload", {}).get("reveals", {})
        seeds: dict = {}
        for cid, r in reveals.items():
            try:
                seed  = bytes.fromhex(r["seed_hex"])
                nonce = bytes.fromhex(r["nonce_hex"])
            except (KeyError, ValueError):
                _log.error("shuffle: malformed reveal for %s — aborting hand", cid)
                if self.on_shuffle_cheat:
                    self.on_shuffle_cheat(cid)
                return
            commit = sr._commits.get(cid)
            if commit is None or not verify_commit(seed, nonce, commit):
                _log.error(
                    "shuffle: reveal for %s does not match its commit — "
                    "possible host cheating; aborting hand", cid)
                if self.on_shuffle_cheat:
                    self.on_shuffle_cheat(cid)
                return
            seeds[cid] = seed

        # Independently reproduce the deck and expose it to the peer's GUI.
        try:
            deck = deterministic_shuffle(derive_master_seed(seeds))
        except ValueError:
            return
        if self.on_shuffle_ready:
            self.on_shuffle_ready(deck)

    def _on_shuffle_deal(self, msg: dict) -> None:
        """Peer: receive encrypted hole cards from the host."""
        if self.on_shuffle_deal:
            self.on_shuffle_deal(msg.get("payload", {}))

    def send_encrypted_hole_cards(self, conn_id: str, seat: int,
                                   cards_str: list) -> None:
        """Host: encrypt and unicast hole cards to exactly one peer.

        Uses the X25519 pubkey supplied by that peer during their shuffle_commit.
        Falls back to plaintext ``deal_private`` if the pubkey is unavailable
        (e.g., the peer is running an older client).
        """

        sr = self._shuffle_round
        pubkey_bytes: bytes | None = None
        if sr is not None:
            pubkey_bytes = sr.x25519_pubkeys.get(conn_id)
        # Also try the stored player record
        if pubkey_bytes is None:
            sp = self.players.get(conn_id)
            if sp and sp.x25519_pubkey_hex:
                try:
                    pubkey_bytes = bytes.fromhex(sp.x25519_pubkey_hex)
                except ValueError:
                    pass

        if pubkey_bytes is not None:
            from holdem.p2p.shuffle import encrypt_hole_cards
            try:
                blob = encrypt_hole_cards(cards_str, pubkey_bytes)
                self._transport.send(conn_id, {
                    "type": "shuffle_deal",
                    "payload": {"seat": seat, "encrypted_hex": blob.hex()},
                })
                return
            except Exception as exc:
                _log.warning(
                    "shuffle: encryption failed for seat %d (%s): %s — "
                    "falling back to plaintext", seat, conn_id, exc)

        # Fallback: legacy plaintext deal (no encryption)
        self._transport.send(conn_id, {
            "type":    "deal_private",
            "payload": {"seat": seat, "hole_cards": cards_str},
        })

    # ------------------------------------------------------------------
    # Disconnect / host migration
    # ------------------------------------------------------------------

    def handle_disconnect(self, conn_id: str) -> None:
        """Called by the transport on_disconnect handler for any dropped peer."""
        with self._lock:
            self.players.pop(conn_id, None)
            if conn_id in self._join_order:
                self._join_order.remove(conn_id)

        if conn_id == self._host_conn_id:
            # The host dropped — elect a new one
            self._elect_new_host()
        else:
            # A non-host peer dropped
            if self.is_host:
                self._broadcast_player_list()
            if self.on_player_list_changed:
                self.on_player_list_changed(list(self.players.values()))

    def _elect_new_host(self) -> None:
        """Lowest-join-order peer becomes the new host."""
        if not self._join_order:
            return
        new_host_conn = self._join_order[0]
        # M-6: do NOT fall through to am_new_host when local_conn_id is "" —
        # a peer that never received player_ack cannot reliably self-identify
        # and promoting every such peer causes split-brain.
        am_new_host = (new_host_conn == self.local_conn_id
                       and self.local_conn_id != "")
        if am_new_host:
            self.is_host = True
            self._host_conn_id = self.local_conn_id
            self._broadcast_player_list()
            if self.state == "PLAYING" and self.on_host_changed:
                self.on_host_changed(True)
            elif self.on_host_changed:
                self.on_host_changed(True)
        else:
            if self.on_host_changed:
                self.on_host_changed(False)

    # ------------------------------------------------------------------
    # Admin message handlers (pause / resume / kick / adjust_blinds)
    # ------------------------------------------------------------------

    def _on_pause(self, conn_id: str, msg: dict) -> None:
        # C-2: only accept admin messages from the host's connection
        if conn_id != self._host_conn_id:
            return
        if not self.is_host and self.on_pause:
            self.on_pause()

    def _on_resume(self, conn_id: str, msg: dict) -> None:
        # C-2: only accept admin messages from the host's connection
        if conn_id != self._host_conn_id:
            return
        if not self.is_host and self.on_resume:
            self.on_resume()

    def _on_kick(self, conn_id: str, msg: dict) -> None:
        # C-2: only accept admin messages from the host's connection
        if conn_id != self._host_conn_id:
            return
        if not self.is_host and self.on_kick:
            self.on_kick(msg.get("payload", {}))

    def _on_adjust_blinds(self, conn_id: str, msg: dict) -> None:
        # C-2: only accept admin messages from the host's connection
        if conn_id != self._host_conn_id:
            return
        if not self.is_host and self.on_adjust_blinds:
            self.on_adjust_blinds(msg.get("payload", {}))

    # ------------------------------------------------------------------
    # Host actions
    # ------------------------------------------------------------------

    def _broadcast_player_list(self) -> None:
        """Send the current player roster to all connected peers (host only)."""
        # Late import to avoid circular dependency with transport module.
        with self._lock:
            players_data = [
                {
                    "conn_id":           p.conn_id,
                    "peer_id":           p.peer_id,
                    "nickname":          p.nickname,
                    "avatar_b64":        p.avatar_b64,
                    "x25519_pubkey_hex": p.x25519_pubkey_hex,
                    "is_host":           p.is_host,
                    "ready":             p.ready,
                }
                for p in self.players.values()
            ]
            snapshot = list(self.players.values())
        self._transport.broadcast({"type": "player_list", "payload": {"players": players_data}})
        if self.on_player_list_changed:
            self.on_player_list_changed(snapshot)

    def add_local_player(self, conn_id: str) -> None:
        """Register the local host player once we know our own conn_id."""
        from holdem.p2p import identity as _id
        with self._lock:
            self.players[conn_id] = Player(
                conn_id           = conn_id,
                peer_id           = "",
                nickname          = self.local_nickname,
                avatar_b64        = self.local_avatar,
                x25519_pubkey_hex = _id.x25519_public_key_bytes().hex(),
                is_host           = self.is_host,
                ready             = True,
            )
        if self.is_host:
            self._broadcast_player_list()

    def set_ready(self, conn_id: str, ready: bool) -> None:
        """Update a player's ready flag; host re-broadcasts the player list."""
        with self._lock:
            if conn_id in self.players:
                self.players[conn_id].ready = ready
        if self.is_host:
            self._broadcast_player_list()

    @property
    def all_ready(self) -> bool:
        """True when every seated player has ready=True and there are ≥ 2."""
        with self._lock:
            players = list(self.players.values())
        return len(players) >= 2 and all(p.ready for p in players)

    def start_game(self, table_settings: dict) -> None:
        """Host starts the game: broadcast game_start and transition to PLAYING."""
        if not self.is_host:
            raise RuntimeError("Only the host can start the game")
        with self._lock:
            seat_order = [p.conn_id for p in self.players.values()]
        self._seat_order = seat_order
        self._last_table_settings = table_settings
        payload = {"table_settings": table_settings, "seat_order": seat_order}
        self._transport.broadcast({"type": "game_start", "payload": payload})
        self.state = "PLAYING"
        if self.on_game_start:
            self.on_game_start(payload)

    # ------------------------------------------------------------------
    # In-game: host engine helpers
    # ------------------------------------------------------------------

    _SUIT_CHARS = "cdhs"
    _RANK_STRS  = {2:"2",3:"3",4:"4",5:"5",6:"6",7:"7",8:"8",9:"9",
                   10:"10",11:"J",12:"Q",13:"K",14:"A"}

    @staticmethod
    def _card_to_str(card) -> str:
        s = Session._SUIT_CHARS
        r = Session._RANK_STRS
        return r[card.v] + s[card.s]

    def broadcast_game_state(self) -> None:
        """Host only: serialize engine state and broadcast to all peers."""
        if not self.is_host or self._engine is None:
            return
        e = self._engine
        state = {
            "street":      e.street,
            "pot":         e.pot,
            "stacks":      [p.stack   for p in e.players],
            "bets":        [p.bet     for p in e.players],
            "community":   [self._card_to_str(c) for c in e.board],
            "folded":      [p.folded  for p in e.players],
            "allin":       [p.all_in  for p in e.players],
            "action_on":   e.actor if e.actor is not None else -1,
            "min_raise":   e.min_raise,
            "call_amount": e.current_bet,
            "hand_num":    e.hand_no,
        }
        self._transport.broadcast({"type": "game_state", "payload": state})

    def send_private_cards(self, conn_id: str, seat: int,
                           hole_cards: list) -> None:
        """Host only: send hole cards to exactly one peer (plaintext legacy)."""
        self._transport.send(conn_id, {
            "type":    "deal_private",
            "payload": {"seat": seat, "hole_cards": hole_cards},
        })

    _VALID_ACTIONS = frozenset(("fold", "call", "raise", "check"))

    def handle_game_action(self, conn_id: str, msg: dict) -> None:
        """Host only: validate and route an action from a peer."""
        if not self.is_host or self._engine is None:
            return
        payload = msg.get("payload", {})
        seat   = payload.get("seat",   -1)
        action = payload.get("action", "fold")
        amount = payload.get("amount", 0)

        # M-9: reject unrecognised action strings before they reach the engine
        if action not in self._VALID_ACTIONS:
            return

        # H-5: inverted guard — reject out-of-range seats AND wrong owner
        if not (0 <= seat < len(self._seat_order)) or self._seat_order[seat] != conn_id:
            return

        if self._engine.actor != seat:
            return
        if self.on_action:
            self.on_action(seat, action, amount)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def peer_count(self) -> int:
        """Number of players currently in the lobby."""
        with self._lock:
            return len(self.players)

    def player_list(self) -> list[Player]:
        with self._lock:
            return list(self.players.values())
