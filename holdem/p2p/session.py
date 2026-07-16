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
host-> all : {"type": "deal",    ...}   (Phase 2 when shuffle is ready)
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, List, Optional


@dataclass
class Player:
    conn_id:    str
    peer_id:    str
    nickname:   str
    avatar_b64: str
    is_host:    bool  = False
    ready:      bool  = False


class Session:
    """Tracks lobby membership and drives the LOBBY -> PLAYING transition."""

    def __init__(self, is_host: bool, nickname: str, avatar_b64: str):
        self.is_host    = is_host
        self.state      = "LOBBY"
        # conn_id -> Player (includes local player once we have a conn_id)
        self.players:   dict[str, Player] = {}
        self.local_nickname  = nickname
        self.local_avatar    = avatar_b64
        self._lock           = threading.Lock()
        self._hash_chain     = "0" * 64

        # UI callbacks -- set by the lobby after constructing the session.
        # Both are called from the transport's background thread; callers
        # should route back to the Tk main thread via root.after(0, ...).
        self.on_player_list_changed: Optional[Callable[[List[Player]], None]] = None
        self.on_game_start:          Optional[Callable[[dict], None]]         = None

    # ------------------------------------------------------------------
    # Message dispatch (called by transport on_message handler)
    # ------------------------------------------------------------------

    def handle_message(self, conn_id: str, msg: dict) -> None:
        """Route an incoming transport message to the appropriate handler."""
        t = msg.get("type")
        if t == "player_info":
            self._on_player_info(conn_id, msg)
        elif t == "player_list":
            self._on_player_list(conn_id, msg)
        elif t == "game_start":
            self._on_game_start(msg)

    def _on_player_info(self, conn_id: str, msg: dict) -> None:
        """Host receives identity from a newly connected peer."""
        payload = msg.get("payload", {})
        with self._lock:
            self.players[conn_id] = Player(
                conn_id    = conn_id,
                peer_id    = msg.get("pubkey", "")[:16],
                nickname   = payload.get("nickname",   "Player"),
                avatar_b64 = payload.get("avatar_b64", ""),
                is_host    = False,
            )
        if self.is_host:
            self._broadcast_player_list()

    def _on_player_list(self, conn_id: str, msg: dict) -> None:
        """Non-host receives updated player list from the host."""
        payload = msg.get("payload", {})
        players_data = payload.get("players", [])
        with self._lock:
            for p in players_data:
                cid = p.get("conn_id", "")
                if cid and cid not in self.players:
                    self.players[cid] = Player(
                        conn_id    = cid,
                        peer_id    = p.get("peer_id",    ""),
                        nickname   = p.get("nickname",   "Player"),
                        avatar_b64 = p.get("avatar_b64", ""),
                        is_host    = p.get("is_host",    False),
                        ready      = p.get("ready",      False),
                    )
            snapshot = list(self.players.values())
        if self.on_player_list_changed:
            self.on_player_list_changed(snapshot)

    def _on_game_start(self, msg: dict) -> None:
        self.state = "PLAYING"
        if self.on_game_start:
            self.on_game_start(msg.get("payload", {}))

    # ------------------------------------------------------------------
    # Host actions
    # ------------------------------------------------------------------

    def _broadcast_player_list(self) -> None:
        """Send the current player roster to all connected peers (host only)."""
        # Late import to avoid circular dependency with transport module.
        from holdem.p2p import transport as _t
        with self._lock:
            players_data = [
                {
                    "conn_id":    p.conn_id,
                    "peer_id":    p.peer_id,
                    "nickname":   p.nickname,
                    "avatar_b64": p.avatar_b64,
                    "is_host":    p.is_host,
                    "ready":      p.ready,
                }
                for p in self.players.values()
            ]
            snapshot = list(self.players.values())
        _t.broadcast({"type": "player_list", "payload": {"players": players_data}})
        if self.on_player_list_changed:
            self.on_player_list_changed(snapshot)

    def add_local_player(self, conn_id: str) -> None:
        """Register the local host player once we know our own conn_id."""
        with self._lock:
            self.players[conn_id] = Player(
                conn_id    = conn_id,
                peer_id    = "",
                nickname   = self.local_nickname,
                avatar_b64 = self.local_avatar,
                is_host    = self.is_host,
                ready      = True,
            )
        if self.is_host:
            self._broadcast_player_list()

    def start_game(self, table_settings: dict) -> None:
        """Host starts the game: broadcast game_start and transition to PLAYING."""
        if not self.is_host:
            raise RuntimeError("Only the host can start the game")
        from holdem.p2p import transport as _t
        with self._lock:
            seat_order = [p.conn_id for p in self.players.values()]
        payload = {"table_settings": table_settings, "seat_order": seat_order}
        _t.broadcast({"type": "game_start", "payload": payload})
        self.state = "PLAYING"
        if self.on_game_start:
            self.on_game_start(payload)

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
