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
    seat_index: int   = -1


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

        # Join order & host tracking
        self._join_order: list[str] = []       # conn_ids in join order (host-side IDs)
        self.local_conn_id: str = ""           # this peer's own conn_id as seen by host
        self._host_conn_id: str = ""           # conn_id used to reach the host (peers only)

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

        # Engine ref (host only) and seat order
        self._engine     = None
        self._seat_order: list[str] = []

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
            self._on_pause(msg)
        elif t == "resume":
            self._on_resume(msg)
        elif t == "kick":
            self._on_kick(conn_id, msg)
        elif t == "adjust_blinds":
            self._on_adjust_blinds(msg)

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
            if conn_id not in self._join_order:
                self._join_order.append(conn_id)
        if self.is_host:
            # Tell the peer their host-side conn_id so they can self-identify
            from holdem.p2p import transport as _t
            _t.send(conn_id, {"type": "player_ack",
                               "payload": {"your_conn_id": conn_id}})
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
        if self.on_game_start:
            self.on_game_start(payload)

    def _on_ready(self, conn_id: str, msg: dict) -> None:
        payload = msg.get("payload", {})
        self.set_ready(conn_id, payload.get("ready", False))

    def _on_game_state(self, msg: dict) -> None:
        if self.on_game_state:
            self.on_game_state(msg.get("payload", {}))

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
            from holdem.p2p import transport as _t
            _t.broadcast(msg)

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
        am_new_host = (new_host_conn == self.local_conn_id
                       or self.local_conn_id == "")
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

    def _on_pause(self, msg: dict) -> None:
        if not self.is_host and self.on_pause:
            self.on_pause()

    def _on_resume(self, msg: dict) -> None:
        if not self.is_host and self.on_resume:
            self.on_resume()

    def _on_kick(self, conn_id: str, msg: dict) -> None:
        if not self.is_host and self.on_kick:
            self.on_kick(msg.get("payload", {}))

    def _on_adjust_blinds(self, msg: dict) -> None:
        if not self.is_host and self.on_adjust_blinds:
            self.on_adjust_blinds(msg.get("payload", {}))

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
        from holdem.p2p import transport as _t
        with self._lock:
            seat_order = [p.conn_id for p in self.players.values()]
        self._seat_order = seat_order
        payload = {"table_settings": table_settings, "seat_order": seat_order}
        _t.broadcast({"type": "game_start", "payload": payload})
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
        from holdem.p2p import transport as _t
        _t.broadcast({"type": "game_state", "payload": state})

    def send_private_cards(self, conn_id: str, seat: int,
                           hole_cards: list) -> None:
        """Host only: send hole cards to exactly one peer."""
        from holdem.p2p import transport as _t
        _t.send(conn_id, {
            "type":    "deal_private",
            "payload": {"seat": seat, "hole_cards": hole_cards},
        })

    def handle_game_action(self, conn_id: str, msg: dict) -> None:
        """Host only: validate and route an action from a peer."""
        if not self.is_host or self._engine is None:
            return
        payload = msg.get("payload", {})
        seat   = payload.get("seat",   -1)
        action = payload.get("action", "fold")
        amount = payload.get("amount", 0)
        # Verify the acting peer owns that seat
        if 0 <= seat < len(self._seat_order):
            if self._seat_order[seat] != conn_id:
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
