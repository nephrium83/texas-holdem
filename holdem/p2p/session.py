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

The old host-coordinated commit-reveal shuffle (Phase 2's 6 shuffle_*
message types) is RETIRED — dealing is the trustless mental-poker deal
(key_announce / deck_round / deal_share / audit_open, see mental_deal.py).
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
    # X25519 pubkey for hole-card encryption (populated from player_info)
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

        # --- mental-poker deal (L5): per-hand coordinator for the local seat ---
        # One driver per hand; created in begin_hand(). The deal is hostless
        # and peer-symmetric, so every peer runs its own driver.
        self._deal_driver = None
        self._deal_outbox: list[dict] = []      # driver emissions buffered for routing
        self._deal_hole: list = [None, None]    # this seat's hole cards (engine Cards)
        self._deal_board: list = [None] * 5     # the board (engine Cards)
        # local device secret for deterministic key shares (crash-survival).
        # NOTE: regenerated per process for now; persisting it across restarts
        # is the separate persistence milestone.
        import os as _os
        self._deal_master_secret = _os.urandom(32)

        # --- hostless betting (L5): per-peer replica engine + orchestration ---
        self._replica = None                    # ReplicaTable for the current hand
        self._own_hole_set = False              # local holes fed to replica yet?
        self._pumping = False                   # re-entrancy guard for _pump_hand
        self.hand_voided = False
        self.void_reason: str | None = None
        self.hand_result: dict | None = None    # normalized settle() result
        # on_hand_settled(result_dict) -- hand settled on this replica
        self.on_hand_settled: Optional[Callable[[dict], None]] = None
        # Hand sequencing over a real async network: there is no host to say
        # "start hand N now", so peers begin hands at slightly different times.
        # Messages carry a hand number; ones for a future hand are buffered and
        # replayed when that hand begins (else an early key_announce is dropped
        # and the deal deadlocks); ones for a past hand are ignored.
        self._hand_no = 0                       # current hand (0 = none begun)
        self._msg_buffer: list = []             # [(conn_id, msg)] for future hands
        # on_state_changed() -- fired after any hand progress, so an async UI
        # can re-render from the local replica on its own thread.
        self.on_state_changed: Optional[Callable[[], None]] = None

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
        # --- mental-poker deal (L5, hostless) ---
        elif t in ("key_announce", "deck_round", "deal_share", "audit_open"):
            self._on_deal_message(conn_id, msg)
        # --- hostless betting (L5): replica action ---
        elif t == "bet_action":
            self._on_bet_action(conn_id, msg)

    # ------------------------------------------------------------------
    # Mental-poker deal (L5) — hostless, peer-symmetric. Each peer drives
    # its own MentalDealDriver for the local seat; messages carry seat
    # indices and are self-describing, so routing is by seat, not conn_id.
    # ------------------------------------------------------------------

    def _deal_session_id(self) -> str:
        """Shared, stable per-game id (every peer holds the same seat order)."""
        return "poker|" + "|".join(self._seat_order)

    def begin_hand(self, hand_no: int, button: int = 0) -> None:
        """Start this seat's mental-poker deal for a hand and kick off the DKG.

        Hostless and peer-symmetric: every peer calls this for the same hand,
        with the same (hand_no, button). All peers must have begun before the
        exchange settles, or an early key_announce would be dropped by a peer
        that has no driver yet.
        """
        from holdem.p2p.mental_deal_driver import MentalDealDriver
        order = list(self._seat_order)
        if self.local_conn_id not in order:
            raise RuntimeError("cannot begin hand: local seat not in seat order")
        self._deal_hole = [None, None]
        self._deal_board = [None] * 5
        self._deal_outbox = []
        self._hand_no = hand_no
        self._deal_driver = MentalDealDriver(
            session_id=self._deal_session_id(),
            hand_no=hand_no,
            local_seat=order.index(self.local_conn_id),
            seats_in=list(range(len(order))),
            button=button,
            master_secret=self._deal_master_secret,
            send=self._deal_outbox.append,      # buffer; _flush_deal routes them
        )
        self._deal_driver.start()
        self._flush_deal()
        self._replay_buffer()

    def _replay_buffer(self) -> None:
        """Feed buffered messages now that a hand has begun: those for the
        current hand are processed, later ones kept, earlier ones dropped."""
        if not self._msg_buffer:
            return
        pending, self._msg_buffer = self._msg_buffer, []
        for cid, m in pending:
            h = m.get("hand", self._hand_no)
            if h == self._hand_no:
                self.handle_message(cid, m)
            elif h > self._hand_no:
                self._msg_buffer.append((cid, m))
            # h < current: stale, dropped

    def _hand_msg_ok(self, conn_id: str, msg: dict) -> bool:
        """Hand-scope filter for deal/bet messages: buffer future-hand ones,
        drop stale ones, admit current-hand ones."""
        h = msg.get("hand", self._hand_no)
        if h > self._hand_no:
            self._msg_buffer.append((conn_id, dict(msg)))
            return False
        return h == self._hand_no

    def _notify_state_changed(self) -> None:
        if self.on_state_changed is not None:
            self.on_state_changed()

    def reveal_board_street(self, street: str) -> None:
        """Reveal a board street ("flop"/"turn"/"river"); called once the
        preceding betting round closes."""
        if self._deal_driver is None:
            return
        self._deal_driver.reveal_street(street)
        self._flush_deal()

    def open_deal_audit(self) -> None:
        """Open the post-hand audit (at showdown)."""
        if self._deal_driver is None:
            return
        self._deal_driver.open_audit()
        self._flush_deal()

    def _on_deal_message(self, conn_id: str, msg: dict) -> None:
        if not self._hand_msg_ok(conn_id, msg):
            return
        if self._deal_driver is None:
            return                              # no active hand yet
        # Seat-spoofing defence: the seat a message claims must be the
        # sender's own seat (the transport already authenticates conn_id).
        order = self._seat_order
        claimed = msg.get("seat", msg.get("seat_from"))
        if conn_id != self.local_conn_id:
            if not (isinstance(claimed, int) and 0 <= claimed < len(order)
                    and order[claimed] == conn_id):
                _log.warning("session: deal msg from %s claims seat %s — dropping",
                             conn_id, claimed)
                return
        self._deal_driver.handle(dict(msg))
        self._flush_deal()

    def _flush_deal(self) -> None:
        """Route buffered driver emissions. Each is broadcast to the OTHER
        peers and also self-delivered to our own driver: the coordinator's
        shuffle chain assumes a peer sees its own broadcast, but the real
        transport excludes the sender, so we feed it back here. Drains to
        quiescence, then pulls any newly recovered cards.

        The outbox list is drained IN PLACE (never rebound): the driver's
        send callback was bound to this exact list object at construction,
        so replacing it would strand later emissions.
        """
        steps = 0
        while self._deal_outbox:
            steps += 1
            if steps > 10000:
                raise RuntimeError("mental-deal flush did not terminate")
            m = self._deal_outbox.pop(0)
            m["hand"] = self._hand_no           # tag for hand-scoped routing
            self._transport.broadcast(m)        # to the other peers
            self._deal_driver.handle(m)         # self-deliver; may append more
        self._apply_deal_cards()
        self._pump_hand()                       # recovered cards may advance the hand
        self._notify_state_changed()

    def _apply_deal_cards(self) -> None:
        if self._deal_driver is None:
            return
        self._deal_hole = self._deal_driver.hole_cards
        self._deal_board = self._deal_driver.board

    @property
    def deal_hole_cards(self) -> list:
        """This seat's hole cards as engine Cards (None until recovered)."""
        return list(self._deal_hole)

    @property
    def deal_board(self) -> list:
        """The board as engine Cards, filling street by street."""
        return list(self._deal_board)

    def deal_done(self) -> bool:
        return self._deal_driver is not None and self._deal_driver.is_done()

    def deal_aborted(self) -> bool:
        return self._deal_driver is not None and self._deal_driver.aborted()

    # ------------------------------------------------------------------
    # Hostless hand orchestration (L5): replica betting + mental deal.
    # Every peer runs the same state machine; nothing here is host-only.
    # ------------------------------------------------------------------

    @property
    def local_seat(self) -> int:
        return self._seat_order.index(self.local_conn_id)

    def start_p2p_hand(self, *, hand_no: int, names: list, stacks: list,
                       sb: int, bb: int, structure: str = "No-Limit",
                       button: int = 0) -> None:
        """Run one fully-hostless hand: replica engine for betting, mental
        deal for the cards. Every peer calls this with the same shared
        config. Orchestration order matters: the replica's start_hand MOVES
        the button (blinds / dead-button rule), and that post-move button
        is what drives the mental deal's deal_map -- so the replica starts
        first and the deal is begun with replica.button."""
        from holdem.p2p.replica_table import ReplicaTable
        self.hand_voided = False
        self.void_reason = None
        self.hand_result = None
        self._own_hole_set = False
        self._replica = ReplicaTable(
            session_id=self._deal_session_id(), hand_no=hand_no,
            names=list(names), stacks=list(stacks), sb=sb, bb=bb,
            structure=structure)
        self._replica.start_hand(button)
        self.begin_hand(hand_no, button=self._replica.button)
        self._pump_hand()

    def send_bet_action(self, action: str, amount: int = 0) -> str:
        """Act for the LOCAL seat: apply to our own replica first, then
        broadcast the action with our post-apply state digest so every
        peer can verify we all agree (desync detection)."""
        if self._replica is None or self.hand_voided:
            return "rejected"
        seat = self.local_seat
        seq = self._replica.next_seq
        verdict = self._replica.apply_action(seq, seat, action, amount)
        if verdict != "applied":
            return verdict
        self._transport.broadcast({
            "type": "bet_action", "hand": self._hand_no, "seq": seq, "seat": seat,
            "action": action, "amount": int(amount),
            "digest": self._replica.state_digest(),
        })
        self._pump_hand()
        self._notify_state_changed()
        return verdict

    def _on_bet_action(self, conn_id: str, msg: dict) -> None:
        if not self._hand_msg_ok(conn_id, msg):
            return
        if self._replica is None or self.hand_voided:
            return
        try:
            seq = int(msg["seq"])
            seat = int(msg["seat"])
            action = str(msg["action"])
            amount = int(msg.get("amount", 0))
        except (KeyError, ValueError, TypeError):
            return
        # seat-spoofing defence, same rule as the deal messages
        order = self._seat_order
        if conn_id != self.local_conn_id:
            if not (0 <= seat < len(order) and order[seat] == conn_id):
                _log.warning("session: bet_action from %s claims seat %s "
                             "— dropping", conn_id, seat)
                return
        verdict = self._replica.apply_action(seq, seat, action, amount)
        if verdict == "applied":
            # Desync detection: the sender attached its post-apply digest.
            # Compare only when we applied exactly that action (a buffered
            # later action draining in the same call would legitimately
            # move our digest past the sender's snapshot).
            theirs = msg.get("digest")
            if (theirs is not None
                    and self._replica.next_seq == seq + 1
                    and theirs != self._replica.state_digest()):
                self._void_hand(f"replica desync detected at action {seq}")
                return
        self._pump_hand()
        self._notify_state_changed()

    def _void_hand(self, reason: str) -> None:
        """Void the hand (desync, deal abort, audit failure). Chips revert
        to their pre-hand state because settle() never ran; a settled hand
        is final and cannot be voided."""
        if self.hand_voided:
            return
        self.hand_voided = True
        self.void_reason = reason
        _log.warning("session: HAND VOIDED — %s", reason)

    def _pump_hand(self) -> None:
        """Advance the hand's orchestration to quiescence: feed recovered
        cards to the replica, reveal and advance streets, open the audit,
        settle. Called after every deal message, bet action, and lifecycle
        call. Re-entrant invocations (reveal/audit go through _flush_deal,
        which calls back here) are absorbed by the guard; the outermost
        pump loops until no step makes progress."""
        if self._pumping or self._replica is None:
            return
        self._pumping = True
        try:
            for _ in range(32):
                if self.hand_voided:
                    return
                if self.deal_aborted():
                    d = self._deal_driver
                    self._void_hand(f"deal aborted: {d.abort_reason} "
                                    f"(seat {d.bad_seat})")
                    return
                if not self._step_hand():
                    return
        finally:
            self._pumping = False

    def _step_hand(self) -> bool:
        """One orchestration step. Returns True iff progress was made."""
        from holdem.p2p.replica_table import (
            PHASE_STREET_OVER, PHASE_SHOWDOWN, PHASE_HAND_OVER)
        r = self._replica
        # 1. local hole cards -> replica, as soon as the deal recovers them
        if not self._own_hole_set:
            hole = self.deal_hole_cards
            if all(c is not None for c in hole):
                r.set_own_hole(self.local_seat, hole)
                self._own_hole_set = True
                return True
        # 2. a betting round closed: reveal the next street, then advance
        #    the replica with the REAL recovered board cards
        if r.phase == PHASE_STREET_OVER:
            street = {"preflop": "flop", "flop": "turn",
                      "turn": "river"}[r.engine.street]
            slots = {"flop": (0, 1, 2), "turn": (3,), "river": (4,)}[street]
            board = self.deal_board
            if not all(board[s] is not None for s in slots):
                self.reveal_board_street(street)      # idempotent; flushes
                board = self.deal_board               # may be complete now
            if all(board[s] is not None for s in slots):
                r.advance_street([board[s] for s in slots])
                return True
            return False               # waiting on other peers' shares
        # 3. hand over (folds) or showdown: audit, then settle
        if r.phase in (PHASE_SHOWDOWN, PHASE_HAND_OVER) and self.hand_result is None:
            if not self.deal_done():
                self.open_deal_audit()                # idempotent; flushes
                if not self.deal_done():
                    return False       # waiting on other peers' openings
            holes = self._deal_driver.all_hole_cards()
            if r.phase == PHASE_SHOWDOWN and holes:
                r.set_all_holes(holes)
            self.hand_result = r.finish(
                force_tabled=(r.phase == PHASE_SHOWDOWN))
            if self.on_hand_settled:
                self.on_hand_settled(self.hand_result)
            return False               # settled: terminal state
        return False

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
