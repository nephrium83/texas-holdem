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

STATE OWNERSHIP — read this before adding a method
--------------------------------------------------
All mutable protocol state on this class has exactly ONE owner: the
SessionOwner held by ``self._owner``. Nothing may mutate players,
_join_order, _host_conn_id, _deal_driver, _replica, the hand record,
_hand_no, state, _seat_order, deadline state, the outbox, or held-message
buffers outside it.

Mutations arrive from four different threads -- the transport dispatch
consumer (inbound messages, connect/disconnect), the UI/main thread (local
commands), whichever thread drives timeouts, and tests. Serialization used
to be customary: it happened to hold because of scheduling, and
terminate()'s check-then-set survived on luck rather than on any
structural guarantee. It is now enforced.

Two mechanisms:

* ``@owned`` on every externally reachable mutating method. A foreign
  thread is MARSHALLED -- it blocks to acquire the owner -- never silently
  let through. This is the supported way in, and it is why callers keep
  their synchronous return contracts.

* ``_assert_owner()`` at inner decision points (_end_hand,
  _elect_new_host, _invalidate_pending_work, terminate). These are only
  reachable from owned contexts, so an unowned caller means a new path
  bypassed the entry points; it raises rather than corrupting state
  quietly.

DO NOT mutate protocol state directly from another thread, and do not
"just add a lock" for a new field. One owner means there is no lock
ordering to remember, which is the only reason this stays correct across
future patches. If a new method mutates protocol state, decorate it
``@owned``; tests/test_session_ownership.py fails if you forget.

The owner is a re-entrant lock rather than a worker thread deliberately:
every caller here is synchronous (send_bet_action returns a verdict, the
in-memory bus delivers inline and inspects immediately), so a queue would
turn each into a future and rewrite the callers rather than fix ownership.
What correctness needs is a total order and atomic check-then-set, and a
single owner supplies both.
"""
from __future__ import annotations

import functools
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from holdem.p2p.timeout import (
    Clock, DeadlineToken, RealClock, DEFAULT_PHASE_TIMEOUTS,
)
from holdem.p2p.events import EventSink, NullSink, SCHEMA_VERSION

_log = logging.getLogger(__name__)

_HOSTLESS_PAYLOAD_TYPES = frozenset({
    "key_announce", "deck_round", "deal_share", "audit_open",
    "bet_action", "hand_void", "session_end",
    "timeout_proposal",
})


@dataclass
class Player:
    conn_id:           str
    peer_id:           str
    nickname:          str
    avatar_b64:        str
    is_host:           bool  = False
    ready:             bool  = False
    unavailable:       bool  = False   # set by timeout; peer stays seated
    seat_index:        int   = -1
    # X25519 pubkey for hole-card encryption (populated from player_info)
    x25519_pubkey_hex: str   = ""
    # Full Ed25519 SIGNING key, hex. Distinct from x25519_pubkey_hex, which
    # is a different key for a different purpose, and from peer_id, which is
    # only the first 16 hex chars and so cannot authenticate anything. This
    # is the protocol-author identity: seats are bound to it, and every
    # seat-scoped message is authorized against it rather than against the
    # connection that delivered it.
    ed25519_pubkey_hex: str  = ""


@dataclass(frozen=True)
class TerminalRecord:
    """Forensic record of the one transition that ended a session.

    Immutable and written exactly once. A later terminal event is a no-op
    and must not replace the original cause -- the first valid transition
    wins, and reconstructing an incident needs to know which one it was.

    Deliberately carries no private cards, secret shares, secret scalars,
    or proof randomness.
    """
    session_id: str
    hand_no: int
    previous_state: str
    terminal_state: str
    terminal_reason: str
    initiating_seat: Optional[int]
    conn_id: Optional[str]
    host_conn_id: str
    monotonic_ts: float
    sequence: int


class SessionOwner:
    """The one serialized execution context for a Session's protocol state.

    Ownership is a re-entrant lock plus explicit owner identity rather than
    a worker thread, for one concrete reason: every caller has a synchronous
    contract. send_bet_action returns "applied"/"rejected", next_p2p_hand
    returns a verdict, and the in-memory test bus delivers inline and
    inspects the result immediately. Marshalling those onto another thread
    would turn each into a future and rewrite the callers rather than fix
    the ownership problem.

    What this provides that "customary" serialization did not:

    * a total order over all mutations, whoever initiates them
    * ONE lock, so there is no lock-ordering discipline to remember and
      therefore none to get wrong in a later patch
    * check-then-set sequences (terminate, _end_hand, _elect_new_host) that
      are atomic by construction rather than by scheduling luck
    * re-entrancy, so an owned method calling another, or a UI callback
      calling back in, does not self-deadlock

    A thread that does not hold it is MARSHALLED -- it blocks to acquire --
    never silently let through. Internal mutators call _assert_owner(), so a
    path that bypasses the entry points raises instead of corrupting state.
    """

    __slots__ = ("_lock", "_thread", "_depth")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread = None
        self._depth = 0

    def __enter__(self) -> "SessionOwner":
        self._lock.acquire()
        if self._depth == 0:
            self._thread = threading.current_thread()
        self._depth += 1
        return self

    def __exit__(self, *exc) -> bool:
        self._depth -= 1
        if self._depth == 0:
            self._thread = None
        self._lock.release()
        return False

    def held(self) -> bool:
        """True iff the calling thread currently owns the session."""
        return self._depth > 0 and self._thread is threading.current_thread()


def owned(method):
    """Run *method* as the session owner, marshalling foreign threads."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._owner:
            return method(self, *args, **kwargs)
    wrapper.__owned__ = True
    return wrapper


@dataclass(frozen=True)
class HandRecord:
    """Forensic record of the one transition that ended a HAND.

    Distinct from TerminalRecord because the two levels have genuinely
    different lifetimes: a voided hand is RECOVERABLE -- next_p2p_hand
    redeals the same seats at the same button and play continues -- whereas
    a terminated session is absorbing. Collapsing them would either make
    void permanent (breaking continuous play) or make session termination
    recoverable (defeating its purpose).

    Written once per hand, then cleared when the next hand begins.
    """
    hand_no: int
    outcome: str
    reason: str
    blamed_seat: Optional[int]
    monotonic_ts: float
    sequence: int


class Session:
    """Tracks lobby membership and drives the LOBBY -> PLAYING transition."""

    #: Session-level terminal outcomes. ABSORBING: once set, no local or
    #: remote entry point may mutate protocol state again. Host loss during
    #: PLAYING is one of them; it is NOT a fold, a timeout, or grounds for
    #: electing a host.
    HOST_LOST = "HOST_LOST"
    ENDED_NORMAL = "ENDED_NORMAL"
    ABORTED_PROTOCOL = "ABORTED_PROTOCOL"
    LOCAL_SHUTDOWN = "LOCAL_SHUTDOWN"

    #: Hand-level outcomes. RECOVERABLE: the session plays on, and a voided
    #: hand is redealt to the same seats at the same button.
    HAND_COMPLETED = "COMPLETED"
    VOID_PROTOCOL = "VOID_PROTOCOL"
    VOID_PEER_LOST = "VOID_PEER_LOST"
    VOID_TIMEOUT = "VOID_TIMEOUT"
    VOID_LOCAL_ABORT = "VOID_LOCAL_ABORT"

    #: Key under table_settings carrying the table-wide shuffle-proof mode.
    #: Absent or false means detection-only, so a table created by an older
    #: build reads as detection-only rather than failing to parse.
    PREVENTION_SETTING = "bg_prevention"

    def __init__(self, is_host: bool, nickname: str, avatar_b64: str,
                 transport=None, clock: Optional[Clock] = None,
                 sink: Optional[EventSink] = None,
                 require_prevention: bool = False,
                 master_secret: Optional[bytes] = None):
        # The one serialized execution context for this session's
        # protocol state. Created first: every field below is only ever
        # mutated while this is held.
        self._owner = SessionOwner()
        self.is_host    = is_host
        # Local policy, NOT table state: refuse to be dealt into a table
        # that is not running Bayer-Groth prevention. The table-wide mode
        # itself arrives in game_start; this only decides whether this peer
        # is willing to play under it. Without it a host could silently
        # downgrade a table to detection-only and nobody would notice.
        self.require_prevention = require_prevention
        # transport module (or a mock) providing broadcast()/send().
        # Defaults to the real global transport; tests inject an
        # in-memory one so N sessions can run in one process.
        if transport is None:
            from holdem.p2p import transport as _t_module
            transport = _t_module
        self._transport = transport

        # --- structured event logging (Phase 4) ---
        self._log_sink: EventSink = sink if sink is not None else NullSink()
        self._last_digest: Optional[str] = None   # previous replica digest

        # --- timeout machinery (Phase 3) ---
        self._clock: Clock = clock if clock is not None else RealClock()
        self._deadline_started_at: Optional[float] = None
        self._current_deadline_token: Optional[DeadlineToken] = None
        self._phase_timeout: dict[str, float] = dict(DEFAULT_PHASE_TIMEOUTS)
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
        # --- terminal state (one transition, first cause wins) ---
        self.terminal_state: Optional[str] = None
        self.terminal_reason: Optional[str] = None
        self.terminal_record: Optional[TerminalRecord] = None
        self.on_session_terminated: Optional[Callable] = None
        self._terminal_seq = 0

        # Table-wide shuffle-proof mode, re-read from every game_start.
        # Held separately from _last_table_settings because that dict is
        # only overwritten when non-empty, which would let a stale True
        # survive into a table that is running detection-only.
        self._prevention: bool = False

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
        # Immutable seat -> Ed25519 signing key (hex). The protocol-author
        # identity: bound once, before the first hand, and never changed.
        self._seat_keys: dict[int, str] = {}

        # --- mental-poker deal (L5): per-hand coordinator for the local seat ---
        # One driver per hand; created in begin_hand(). The deal is hostless
        # and peer-symmetric, so every peer runs its own driver.
        self._deal_driver = None
        self._deal_outbox: list[dict] = []      # driver emissions buffered for routing
        self._deal_hole: list = [None, None]    # this seat's hole cards (engine Cards)
        self._deal_board: list = [None] * 5     # the board (engine Cards)
        # Local device secret for deterministic key shares. Persisted, so a
        # crashed and reopened app rederives the SAME share for a seat --
        # see device_secret.py. Loaded lazily on first use rather than in
        # __init__, so merely constructing a Session touches no filesystem.
        self._master_secret_override = master_secret
        self._master_secret_cache: Optional[bytes] = None

        # --- hostless betting (L5): per-peer replica engine + orchestration ---
        self._replica = None                    # ReplicaTable for the current hand
        self._own_hole_set = False              # local holes fed to replica yet?
        self._pumping = False                   # re-entrancy guard for _pump_hand
        self._hand_record: Optional[HandRecord] = None
        self._hand_seq = 0
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
        # Continuous-session state (next_p2p_hand): the shared table
        # config, the CURRENT hand's inputs (the revert point for a
        # voided hand's redeal), and the terminal flags.
        self._table_cfg: dict | None = None
        self._hand_stacks: list | None = None
        self._hand_positions: tuple | None = None
        self._session_over = False
        self._session_winner: int | None = None
        self._final_stacks: list | None = None
        self._session_end_announced = False
        self._p2p_spectator = False
        # on_state_changed() -- fired after any hand progress, so an async UI
        # can re-render from the local replica on its own thread.
        self.on_state_changed: Optional[Callable[[], None]] = None

        self._safe_emit("sidecar_started")

    # ------------------------------------------------------------------
    # Structured event logging helper
    # ------------------------------------------------------------------

    def _safe_emit(self, event: str, **extra) -> None:
        """Emit one JSONL state event to the configured sink.

        Common fields are assembled automatically; callers add hand/seq/phase/
        digest and any event-specific fields via keyword arguments.

        The outer try/except is load-bearing, not defensive boilerplate.
        In a replicated state machine, every code path that mutates engine
        or replica state must be atomic from the perspective of the other
        peers.  A broken or slow sink (misconfigured stdout, disk full, etc.)
        must never prevent apply_action, _void_hand, or any timeout handler
        from completing — those paths must converge identically on every node
        whether logging succeeds or not.  Swallowing sink exceptions here is
        the contract; do not remove the bare except.
        """
        import time as _time
        try:
            payload: dict = {
                "v":     SCHEMA_VERSION,
                "type":  "state_event",
                "ts":    _time.time(),
                "peer":  self.local_conn_id or "",
                "event": event,
            }
            payload.update(extra)
            self._log_sink.emit(payload)
        except Exception:
            pass   # logging must never crash the game

    # ------------------------------------------------------------------
    # Message dispatch (called by transport on_message handler)
    # ------------------------------------------------------------------

    @owned
    def handle_message(self, conn_id: str, msg: dict) -> None:
        """Route an incoming transport message to the appropriate handler."""
        if self.terminal_state is not None:
            # A terminated session accepts no further protocol mutation.
            # Messages already in flight when the session ended arrive here
            # and must be inert rather than reviving a hand nobody is
            # playing any more.
            _log.debug("session: dropping %s from %s — session is %s",
                       msg.get("type"), conn_id, self.terminal_state)
            return
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
        body = self._hostless_body(msg) if t in _HOSTLESS_PAYLOAD_TYPES else msg
        if t == "player_info":
            self._on_player_info(conn_id, msg)
        elif t == "player_list":
            self._on_player_list(conn_id, msg)
        elif t == "player_ack":
            self._on_player_ack(conn_id, msg)
        elif t == "game_start":
            self._on_game_start(conn_id, msg)
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
            self._on_deal_message(conn_id, body)
        # --- hostless betting (L5): replica action ---
        elif t == "bet_action":
            self._on_bet_action(conn_id, body)
        elif t == "hand_void":
            self._on_hand_void(conn_id, body)
        elif t == "session_end":
            self._on_session_end(conn_id, body)
        elif t == "timeout_proposal":
            self._on_timeout_proposal(conn_id, body)

    @staticmethod
    def _hostless_body(msg: dict) -> dict:
        """Unwrap a verified wire envelope for the hostless state machines.

        The coordinator uses flat dictionaries internally. Real transport
        carries those fields inside the signed ``payload`` envelope, while
        the in-memory harness delivers the flat form directly.
        """
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            return dict(msg)
        body = dict(payload)
        body["type"] = msg.get("type")
        # Carry the ENVELOPE's author through the unwrap. Seat authority is
        # the signing key, and it lives on the envelope, not in the payload
        # -- dropping it here would make every remote hostless message
        # unauthorizable. A payload-supplied "pubkey" must never win: the
        # envelope's is the one wire.unpack verified.
        if "pubkey" in msg:
            body["pubkey"] = msg["pubkey"]
        else:
            body.pop("pubkey", None)
        return body

    # ------------------------------------------------------------------
    # Mental-poker deal (L5) — hostless, peer-symmetric. Each peer drives
    # its own MentalDealDriver for the local seat; messages carry seat
    # indices and are self-describing, so routing is by seat, not conn_id.
    # ------------------------------------------------------------------

    def _deal_session_id(self) -> str:
        """Shared, stable per-game id (every peer holds the same seat order)."""
        return "poker|" + "|".join(self._seat_order)

    @property
    def _deal_master_secret(self) -> bytes:
        """This device's secret for deriving key shares.

        Read from disk on first use and cached. An explicitly supplied
        ``master_secret`` wins, which is how tests get isolation and how a
        caller managing its own key material opts out of the file.
        """
        if self._master_secret_override is not None:
            return self._master_secret_override
        if self._master_secret_cache is None:
            from holdem.p2p import device_secret
            self._master_secret_cache = device_secret.load_or_create()
        return self._master_secret_cache

    @_deal_master_secret.setter
    def _deal_master_secret(self, secret: bytes) -> None:
        """Override the device secret after construction.

        Equivalent to passing ``master_secret=`` to __init__; kept as a
        setter because several tests assign stable per-seat secrets to make
        deals reproducible, and because a caller holding its own key
        material should not be forced to decide before constructing.
        """
        self._master_secret_override = secret

    @property
    def prevention(self) -> bool:
        """Whether this table runs Bayer-Groth shuffle proofs.

        Table-wide and uniform by construction: it rides in the same
        game_start table_settings every peer already receives, so peers do
        not negotiate and cannot disagree unless one is compromised or
        running a different build. A peer that disagrees produces or
        expects a proof the others do not, and the hand voids fail-closed
        rather than silently dropping to detection-only.

        Defaults to False when the key is absent, which is what a table
        created by an older build looks like.
        """
        return self._prevention

    @owned
    def begin_hand(self, hand_no: int, button: int = 0,
                   seats_in: Optional[list] = None) -> None:
        """Start this seat's mental-poker deal for a hand and kick off the DKG.

        Hostless and peer-symmetric: every peer calls this for the same hand,
        with the same (hand_no, button, seats_in). All participating peers
        must have begun before the exchange settles, or an early key_announce
        would be dropped by a peer that has no driver yet. `seats_in` is the
        set of seat indices dealt into the hand (default: every seat); busted
        seats are excluded by the caller and take no part in the deal.
        """
        if self.terminal_state is not None:
            # A terminated session must not acquire a live deal driver: it
            # would run a hand no peer agreed to, under a session id derived
            # from state the table has already abandoned.
            raise RuntimeError(
                f"cannot begin a hand: session terminated "
                f"({self.terminal_state}: {self.terminal_reason})")
        from holdem.p2p.mental_deal_driver import MentalDealDriver
        order = list(self._seat_order)
        if self.local_conn_id not in order:
            raise RuntimeError("cannot begin hand: local seat not in seat order")
        if seats_in is None:
            seats_in = list(range(len(order)))
        local = order.index(self.local_conn_id)
        if local not in seats_in:
            raise RuntimeError("cannot begin hand: local seat is not dealt in "
                               "(busted seats spectate via next_p2p_hand)")
        prevention = self.prevention
        if self.require_prevention and not prevention:
            # Fail closed rather than play on a downgraded table: a host
            # that omits the setting would otherwise turn prevention off
            # for everyone without any peer noticing.
            raise RuntimeError(
                "cannot begin hand: this peer requires Bayer-Groth "
                "prevention but the table is running detection-only")
        self._deal_hole = [None, None]
        self._deal_board = [None] * 5
        self._deal_outbox = []
        self._hand_no = hand_no
        self._deal_driver = MentalDealDriver(
            session_id=self._deal_session_id(),
            hand_no=hand_no,
            local_seat=local,
            seats_in=list(seats_in),
            button=button,
            master_secret=self._deal_master_secret,
            send=self._deal_outbox.append,      # buffer; _flush_deal routes them
            prevention=prevention,
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

    def _bind_seat_keys(self) -> None:
        """Freeze seat -> signing key before the first hand.

        Built from the host-authoritative seat order and the roster's
        ed25519_pubkey_hex, which the host took from each joiner's VERIFIED
        envelope rather than from anything a joiner could assert about
        itself.

        Idempotent and one-way: once populated it is never rebuilt, so a
        later roster edit -- including one from a compromised or buggy host
        -- cannot move a seat onto a different key mid-session. A seat that
        cannot be resolved is simply absent from the table, and messages
        claiming it are refused rather than silently trusted.
        """
        if self._seat_keys:
            return                                # already frozen
        bound: dict[int, str] = {}
        with self._lock:
            for seat, cid in enumerate(self._seat_order):
                player = self.players.get(cid)
                key = getattr(player, "ed25519_pubkey_hex", "") if player else ""
                if key:
                    bound[seat] = key
        if bound:
            self._seat_keys = bound

    def _seat_author_ok(self, conn_id: str, msg: dict, seat: int) -> bool:
        """Is this message authorized to act for ``seat``?

        Two independent facts, and the delivering connection is neither:

        * the envelope's signature is valid -- already enforced by
          wire.unpack, which refuses to hand up anything it could not
          verify against the key the envelope names;
        * that key is the one bound to the seat the payload claims.

        conn_id deliberately does NOT participate. That is what makes this
        relay-compatible: once the host forwards B's envelope to C, the
        delivering connection belongs to the host while the author is still
        B, and an authorization rule keyed on the connection would reject
        every relayed message or, worse, attribute it to the host.

        Self-delivery is exempt: the local driver feeds its own emissions
        back without a wire round-trip, so there is no envelope to check.

        Fallback: with no binding established there is no author identity
        to check against, and the pre-existing conn_id rule applies. That
        is the in-memory harness, which carries no envelopes at all. On the
        production path it is unreachable -- wire.unpack requires a pubkey
        field on every message, and the host publishes signing keys for
        every seat -- but it is a fallback rather than a hole and is
        recorded as such.
        """
        if not (0 <= seat < len(self._seat_order)):
            return False
        if conn_id == self.local_conn_id:
            return True
        if self._seat_keys:
            author = msg.get("pubkey")
            expected = self._seat_keys.get(seat)
            return bool(expected) and isinstance(author, str) and author == expected
        return self._seat_order[seat] == conn_id

    def _hand_msg_ok(self, conn_id: str, msg: dict) -> bool:
        """Hand-scope filter for deal/bet messages: buffer future-hand ones,
        drop stale ones, admit current-hand ones. A busted spectator drops
        everything -- it plays no further hands, so buffering would only
        accumulate."""
        if self._p2p_spectator:
            return False
        raw_hand = msg.get("hand", self._hand_no)
        if isinstance(raw_hand, bool):
            return False
        try:
            h = int(raw_hand)
        except (TypeError, ValueError):
            _log.warning("session: malformed hand number ignored")
            return False
        if h > self._hand_no:
            buffered = dict(msg)
            buffered["hand"] = h
            self._msg_buffer.append((conn_id, buffered))
            return False
        return h == self._hand_no

    @owned
    def _notify_state_changed(self) -> None:
        self._maybe_start_deadline()
        if self._replica is not None:
            new_digest = self._replica.state_digest()
            if new_digest != self._last_digest:
                self._safe_emit(
                    "digest_changed",
                    hand       = self._hand_no,
                    seq        = self._replica.next_seq,
                    phase      = self._replica.phase,
                    old_digest = self._last_digest or "",
                    new_digest = new_digest,
                )
                self._last_digest = new_digest
        if self.on_state_changed is not None:
            self.on_state_changed()

    @owned
    def reveal_board_street(self, street: str) -> None:
        """Reveal a board street ("flop"/"turn"/"river"); called once the
        preceding betting round closes."""
        if self.terminal_state is not None or self._deal_driver is None:
            return
        self._deal_driver.reveal_street(street)
        self._flush_deal()

    @owned
    def open_deal_audit(self) -> None:
        """Open the post-hand audit (at showdown)."""
        if self.terminal_state is not None or self._deal_driver is None:
            return
        self._deal_driver.open_audit()
        self._flush_deal()

    def _on_deal_message(self, conn_id: str, msg: dict) -> None:
        if not self._hand_msg_ok(conn_id, msg):
            return
        if self._deal_driver is None or self.hand_voided:
            return                              # no active hand yet
        # Seat-spoofing defence: the seat a message claims must be the
        # sender's own seat (the transport already authenticates conn_id).
        claimed = msg.get("seat", msg.get("seat_from"))
        if not (isinstance(claimed, int)
                and self._seat_author_ok(conn_id, msg, claimed)):
            _log.warning("session: deal msg via %s claims seat %s but is not "
                         "signed by that seat's bound key — dropping",
                         conn_id, claimed)
            return
        self._deal_driver.handle(dict(msg))
        self._flush_deal()

    def _on_hand_void(self, conn_id: str, msg: dict) -> None:
        """Fail the current hand closed when any authenticated seat voids it."""
        if not self._hand_msg_ok(conn_id, msg):
            return
        try:
            seat = int(msg["seat"])
        except (KeyError, TypeError, ValueError):
            return
        if not self._seat_author_ok(conn_id, msg, seat):
            _log.warning("session: hand_void via %s claims seat %s but is "
                         "not signed by that seat's bound key -- dropping",
                         conn_id, seat)
            return
        reason = str(msg.get("reason", "peer voided the hand"))[:512]
        self._void_hand(reason, announce=False)

    def _on_session_end(self, conn_id: str, msg: dict) -> None:
        """Receive final match state, including on already-busted spectators."""
        try:
            seat = int(msg["seat"])
            hand = int(msg["hand"])
            stacks = [int(v) for v in msg["stacks"]]
            raw_winner = msg.get("winner")
            winner = None if raw_winner is None else int(raw_winner)
        except (KeyError, TypeError, ValueError):
            return
        if not self._seat_author_ok(conn_id, msg, seat):
            _log.warning("session: session_end via %s claims seat %s but is "
                         "not signed by that seat's bound key -- dropping",
                         conn_id, seat)
            return
        if hand < self._hand_no or len(stacks) != len(self._seat_order):
            return
        if any(stack < 0 for stack in stacks):
            return
        expected_total = (self._table_cfg or {}).get("total_chips")
        if expected_total is not None and sum(stacks) != expected_total:
            _log.warning("session: session_end has wrong chip total -- dropping")
            return
        alive = [i for i, stack in enumerate(stacks) if stack > 0]
        expected_winner = alive[0] if len(alive) == 1 else None
        if len(alive) > 1 or winner != expected_winner:
            return
        self._finish_session(stacks, announce=False)

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

    @owned
    def start_p2p_hand(self, *, hand_no: int, names: list, stacks: list,
                       sb: int, bb: int, structure: str = "No-Limit",
                       button: int = 0) -> None:
        """Begin a continuous hostless session and run its first hand:
        replica engine for betting, mental deal for the cards. Every peer
        calls this with the same shared config; each LATER hand is started
        with next_p2p_hand(). Orchestration order matters: the replica's
        start_hand MOVES the button (blinds / dead-button rule), and that
        post-move button is what drives the mental deal's deal_map -- so
        the replica starts first and the deal is begun with
        replica.button."""
        if self.terminal_state is not None:
            raise RuntimeError(
                f"cannot start a hand: session terminated "
                f"({self.terminal_state}: {self.terminal_reason})")
        self._table_cfg = {"names": list(names), "sb": int(sb),
                           "bb": int(bb), "structure": structure,
                           "button": int(button),
                           "total_chips": sum(int(stack) for stack in stacks)}
        self._session_over = False
        self._session_winner = None
        self._final_stacks = None
        self._session_end_announced = False
        self._p2p_spectator = False
        self._bind_seat_keys()
        self._begin_p2p_hand(hand_no=hand_no, stacks=list(stacks),
                             positions=None)

    def _begin_p2p_hand(self, *, hand_no: int, stacks: list,
                        positions) -> bool:
        """Construct and start one hand of the continuous session from
        explicit inputs. `positions` is the previous hand's played
        (bb_seat, sb_seat, button) triple -- the dead-button chain state --
        or None for the first hand (and a first-hand redeal)."""
        from holdem.p2p.replica_table import ReplicaTable
        cfg = self._table_cfg
        self._hand_record = None
        self.void_reason = None
        self.hand_result = None
        self._own_hole_set = False
        self._hand_stacks = list(stacks)
        self._hand_positions = positions
        self._replica = ReplicaTable(
            session_id=self._deal_session_id(), hand_no=hand_no,
            names=list(cfg["names"]), stacks=list(stacks),
            sb=cfg["sb"], bb=cfg["bb"], structure=cfg["structure"])
        if positions is None:
            ok = self._replica.start_hand(cfg["button"])
        else:
            bb_seat, sb_seat, btn = positions
            ok = self._replica.start_hand(btn, bb_seat=bb_seat,
                                          sb_seat=sb_seat)
        if not ok:                              # fewer than 2 seats dealt
            self._replica = None
            self._finish_session(stacks, announce=False)
            return False
        self.begin_hand(hand_no, button=self._replica.button,
                        seats_in=self._replica.seats_dealt)
        self._last_digest = self._replica.state_digest()
        self._safe_emit(
            "hand_started",
            hand   = self._hand_no,
            seq    = self._replica.next_seq,
            phase  = self._replica.phase,
            digest = self._last_digest,
        )
        self._pump_hand()
        return True

    @owned
    def next_p2p_hand(self) -> str:
        """Advance the continuous session to its next hand. Every peer
        calls this once the previous hand has settled or voided; identical
        replicas mean identical next-hand inputs on every peer, and
        hand-scoped message buffering absorbs any call-order skew.

        Returns:
          "started"      -- the next hand's deal is underway
          "session_over" -- at most one seat still has chips; no next hand
                            (session_winner holds that seat, if any)
          "eliminated"   -- the LOCAL seat busted: this session stops
                            playing and drops later hands' gameplay messages;
                            final lifecycle updates are still accepted
          "not_ready"    -- the previous hand is still in progress
        """
        if self.terminal_state is not None:
            # A terminated session has no next hand. Reported as
            # session_over rather than raising, because callers already
            # handle that verdict as "stop playing".
            return "session_over"
        if self._table_cfg is None or self._replica is None:
            return "not_ready"
        if self._session_over:
            return "session_over"
        if self._p2p_spectator:
            return "eliminated"
        voided = self.hand_voided
        if not voided and self.hand_result is None:
            return "not_ready"
        if voided:
            # Chips reverted (settle never ran); redeal the same seats
            # with the same button, a live room's misdeal rule: re-running
            # the position advance from the SAME previous chain state
            # reproduces the voided hand's positions exactly.
            stacks = list(self._hand_stacks)
            positions = self._hand_positions
        else:
            stacks = self._replica.stacks       # settled: identical everywhere
            positions = self._replica.positions  # played chain state
        alive = [i for i, s in enumerate(stacks) if s > 0]
        if len(alive) < 2:
            self._finish_session(stacks, announce=True)
            return "session_over"
        if self.local_seat not in alive:
            # Busted: stop playing. The final settled snapshot (replica,
            # result, reveals) is retained for the client. Gameplay messages
            # for later hands are dropped, while session_end remains accepted.
            self._p2p_spectator = True
            self._msg_buffer.clear()
            self._notify_state_changed()
            return "eliminated"
        started = self._begin_p2p_hand(hand_no=self._hand_no + 1,
                                       stacks=stacks, positions=positions)
        return "started" if started else "session_over"

    def _finish_session(self, stacks: list, *, announce: bool) -> None:
        """Record final stacks and optionally announce them to all spectators."""
        final = [int(stack) for stack in stacks]
        alive = [i for i, stack in enumerate(final) if stack > 0]
        winner = alive[0] if len(alive) == 1 else None
        if self._session_over:
            if self._final_stacks is not None and self._final_stacks != final:
                _log.warning("session: conflicting session_end ignored")
            return
        self._session_over = True
        self._session_winner = winner
        self._final_stacks = final
        self._p2p_spectator = self.local_seat not in alive
        self._notify_state_changed()
        # A completed match permanently ends the session, so it goes through
        # the one terminal mechanism. terminate() clears the message buffer,
        # cancels the deadline, and emits sidecar_stopping.
        self.terminate(
            self.ENDED_NORMAL,
            f"match complete; winner seat {winner}"
            if winner is not None else "match complete",
            event={"hand": self._hand_no, "reason": "session_complete",
                   "winner": winner})
        if announce and not self._session_end_announced:
            self._session_end_announced = True
            self._transport.broadcast({
                "type": "session_end",
                "hand": self._hand_no,
                "seat": self.local_seat,
                "winner": winner,
                "stacks": final,
            })

    @owned
    def send_bet_action(self, action: str, amount: int = 0) -> str:
        """Act for the LOCAL seat: apply to our own replica first, then
        broadcast the action with our post-apply state digest so every
        peer can verify we all agree (desync detection)."""
        if self.terminal_state is not None:
            # A terminated session must not apply to its replica, advance
            # local betting state, or broadcast. Broadcasting here injected
            # actions into a table this peer had already left, which is a
            # desync source and not merely a local inconsistency.
            return "rejected"
        if self._replica is None or self.hand_voided:
            return "rejected"
        seat = self.local_seat
        seq = self._replica.next_seq
        verdict = self._replica.apply_action(seq, seat, action, amount)
        if verdict != "applied":
            return verdict
        self._safe_emit(
            "action_applied",
            hand   = self._hand_no,
            seq    = self._replica.next_seq,
            phase  = self._replica.phase,
            digest = self._replica.state_digest(),
            seat   = seat, action = action, amount = int(amount),
        )
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
        if not self._seat_author_ok(conn_id, msg, seat):
            _log.warning("session: bet_action via %s claims seat %s but is not "
                         "signed by that seat's bound key — dropping",
                         conn_id, seat)
            return
        self._safe_emit(
            "action_received",
            hand=self._hand_no, seq=seq, seat=seat,
            action=action, amount=amount,
        )
        verdict = self._replica.apply_action(seq, seat, action, amount)
        if verdict == "applied":
            self._safe_emit(
                "action_applied",
                hand   = self._hand_no,
                seq    = self._replica.next_seq,
                phase  = self._replica.phase,
                digest = self._replica.state_digest(),
                seat   = seat, action = action, amount = amount,
            )
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

    def _assert_owner(self) -> None:
        """Fail loudly if protocol state is mutated outside the owner.

        Every externally reachable mutator is @owned, so reaching one of
        these unowned means a new call path bypassed the entry points --
        exactly the drift this exists to catch, and why ownership is
        asserted rather than assumed.
        """
        if not self._owner.held():
            raise RuntimeError(
                "session protocol state mutated outside the owner "
                f"(thread {threading.current_thread().name!r}); route the "
                "call through an @owned entry point")

    @property
    def hand_voided(self) -> bool:
        """Derived from the hand record; not an independent truth source.

        This used to be the authoritative void flag -- assigned in one
        place, read in eleven -- which made hand termination a second
        shutdown path with no record and no first-cause-wins guarantee. It
        now reports what _end_hand decided.
        """
        rec = self._hand_record
        return rec is not None and rec.outcome != self.HAND_COMPLETED

    @property
    def hand_record(self) -> Optional["HandRecord"]:
        """The immutable record of how the current hand ended, or None."""
        return self._hand_record

    def _end_hand(self, outcome: str, reason: str, *,
                  blamed_seat: Optional[int] = None,
                  announce: bool = True) -> bool:
        """The single hand-terminal transition. True if this call won.

        Hand-level rather than session-level because a void is RECOVERABLE:
        next_p2p_hand redeals the same seats at the same button and play
        continues. Routing it through terminate() would make every void
        permanently end the session and break continuous play.

        Idempotent per hand, first cause wins, exactly one record and one
        notification per hand. A terminated session ends no further hands.
        """
        self._assert_owner()
        if self.terminal_state is not None:
            return False
        if self._hand_record is not None or self.hand_result is not None:
            return False
        self._hand_seq += 1
        self.void_reason = str(reason)[:512]
        self._hand_record = HandRecord(
            hand_no=self._hand_no,
            outcome=outcome,
            reason=self.void_reason,
            blamed_seat=blamed_seat,
            monotonic_ts=time.monotonic(),
            sequence=self._hand_seq,
        )
        if outcome == self.HAND_COMPLETED:
            return True

        _log.warning("session: HAND VOIDED - %s", reason)
        self._safe_emit("hand_voided", hand=self._hand_no,
                        reason=self.void_reason, outcome=outcome)
        self._notify_state_changed()
        if announce:
            self._transport.broadcast({
                "type": "hand_void",
                "hand": self._hand_no,
                "seat": self.local_seat,
                "reason": self.void_reason,
            })
        return True

    @owned
    def _void_hand(self, reason: str, *, announce: bool = True,
                   outcome: str = "VOID_PROTOCOL",
                   blamed_seat: Optional[int] = None) -> bool:
        """Void this hand and retain its pre-hand redeal inputs.

        Retained as the callers' entry point; the decision now lives in
        _end_hand so hand termination has exactly one implementation.
        """
        return self._end_hand(outcome, reason, blamed_seat=blamed_seat,
                              announce=announce)

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
        nickname = payload.get("nickname", "Player")
        with self._lock:
            self.players[conn_id] = Player(
                conn_id           = conn_id,
                peer_id           = msg.get("pubkey", "")[:16],
                nickname          = nickname,
                avatar_b64        = payload.get("avatar_b64",         ""),
                x25519_pubkey_hex = payload.get("x25519_pubkey_hex",  ""),
                # From the ENVELOPE, not the payload: wire.unpack has already
                # verified the signature against this key, so it is the one
                # fact about a joiner nobody can assert on its behalf.
                ed25519_pubkey_hex = msg.get("pubkey", ""),
                is_host           = False,
            )
            if conn_id not in self._join_order:
                self._join_order.append(conn_id)
        self._safe_emit("peer_connected", conn_id=conn_id, nickname=nickname)
        if self.is_host:
            # Tell the peer their host-side conn_id so they can self-identify
            self._transport.send(conn_id, {"type": "player_ack",
                               "payload": {"your_conn_id": conn_id}})
            self._broadcast_player_list()

    def _on_player_list(self, conn_id: str, msg: dict) -> None:
        """Non-host receives updated player list from the host.

        Host-gated, like _on_game_start and _on_player_ack. This one was
        not, so any connected peer could inject roster entries and
        overwrite nickname, is_host, ready, the x25519 key and now the
        SIGNING key -- which would let an attacker choose the key a victim
        binds to a seat, defeating author authentication before it began.

        A hop-level check is the right kind here: player_list is
        host-authored by definition, so the authority being tested really
        is "which connection", not "which author".
        """
        if self._host_conn_id and conn_id != self._host_conn_id:
            _log.warning("session: player_list from non-host %s — ignoring",
                         conn_id)
            return
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
                        ed25519_pubkey_hex = p.get("ed25519_pubkey_hex", ""),
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
                    existing.ed25519_pubkey_hex = p.get(
                        "ed25519_pubkey_hex", existing.ed25519_pubkey_hex)
            # Mirror join order from the host's authoritative list (non-hosts only)
            self._join_order = [
                p.get("conn_id", "") for p in players_data
                if p.get("conn_id", "") and not p.get("is_host", False)
            ]
            snapshot = list(self.players.values())
        if self.on_player_list_changed:
            self.on_player_list_changed(snapshot)

    def _on_player_ack(self, conn_id: str, msg: dict) -> None:
        """Peer receives its own host-side conn_id from the host.

        This is the ONLY other writer of _host_conn_id besides the (now
        lobby-gated) election, and it previously accepted the assignment
        from any sender in any state. Host identity is the authorization
        token for every host-gated handler -- pause, resume, kick,
        adjust_blinds, and session_end all check
        ``conn_id == self._host_conn_id`` -- so one unsolicited player_ack
        from a seated peer relocated that check onto the sender, mid-hand,
        in the window host identity is meant to be frozen. It also
        overwrote local_conn_id, which feeds the seat-spoof check and
        _deal_session_id.

        Three conditions now, all necessary:

        * LOBBY only. Host identity is immutable once play begins.
        * From the host, or from anyone only while the host is still
          unknown -- this message is legitimately how a joining peer first
          learns who the host is, so it cannot require a known host.
        * A usable conn_id, so a malformed payload cannot blank out this
          peer's own identity.
        """
        if self.terminal_state is not None or self.state != "LOBBY":
            _log.warning("session: player_ack from %s ignored in state %s",
                         conn_id, self.terminal_state or self.state)
            return
        if self._host_conn_id and conn_id != self._host_conn_id:
            _log.warning("session: player_ack from non-host %s — ignoring",
                         conn_id)
            return
        assigned = msg.get("payload", {}).get("your_conn_id")
        if not isinstance(assigned, str) or not assigned:
            _log.warning("session: player_ack from %s carried no usable "
                         "conn_id — ignoring", conn_id)
            return
        self.local_conn_id = assigned
        self._host_conn_id = conn_id   # conn_id of the connection to the host

    @owned
    def _on_game_start(self, conn_id: str, msg: dict) -> None:
        """Adopt the host's table settings -- once, from the host only.

        This message defines seat order and the table-wide prevention mode,
        and both were previously rewritten by any peer at any time. A
        forged game_start mid-hand could repoint every seat index (which
        reassigns hole cards and blame) or turn prevention off for this
        peer, a downgrade nothing else would notice because prevention is
        read from exactly this message.

        Capabilities are therefore frozen once play begins. A duplicate of
        the legitimate message is accepted as a no-op rather than refused,
        so retries and relay echoes stay harmless.
        """
        payload = msg.get("payload", {})
        if self._host_conn_id and conn_id != self._host_conn_id:
            _log.warning("session: game_start from non-host %s — ignoring",
                         conn_id)
            return
        if self.state == "PLAYING" or self.terminal_state is not None:
            # Keyed on terminality as well as state: terminate() sets state
            # to ENDED, so a freeze conditioned on PLAYING alone silently
            # lapses the moment the session dies -- masked today only by the
            # handle_message guard, and that guard is expected to relax for
            # teardown messages.
            settings = payload.get("table_settings", {})
            same = (list(payload.get("seat_order", [])) == list(self._seat_order)
                    and bool(settings.get(self.PREVENTION_SETTING, False))
                        == self._prevention)
            if not same:
                _log.warning(
                    "session: game_start from %s would change settled table "
                    "settings mid-session — ignoring", conn_id)
            return
        self.state = "PLAYING"
        self._seat_order = payload.get("seat_order", [])
        # Store table settings so _mp_new_game in gui.py can read them
        ts = payload.get("table_settings", {})
        if ts:
            self._last_table_settings = ts
        # Read unconditionally: an absent key means detection-only, and
        # must clear any mode carried over from an earlier table.
        self._prevention = bool(ts.get(self.PREVENTION_SETTING, False))
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

    @owned
    def terminate(self, state: str, reason: str, *,
                  conn_id: Optional[str] = None,
                  seat: Optional[int] = None,
                  event: Optional[dict] = None) -> bool:
        """The single terminal transition. True if this call is the winner.

        Idempotent by construction: once a terminal state is set, every
        later call is a no-op that leaves the original cause intact. That
        matters because several subsystems can each notice the same failure
        (a disconnect, a timeout, and a peer's hand_void), and without one
        winner they would each half-terminate and record contradictory
        reasons.

        Runs on the owner thread. All transport-originated callers already
        arrive on the dispatch consumer; local callers must too.
        """
        self._assert_owner()
        # Atomic by construction: @owned holds the owner across this
        # check and the assignment below, so two callers cannot both
        # observe None. This was previously an unlocked check-then-set
        # that happened to survive scheduling, which is not the same as
        # being correct.
        if self.terminal_state is not None:
            _log.debug("session: %s ignored — already terminal (%s)",
                       state, self.terminal_state)
            return False

        previous, self.state = self.state, "ENDED"
        self.terminal_state = state
        self.terminal_reason = reason
        self._terminal_seq += 1
        self.terminal_record = TerminalRecord(
            session_id=self._deal_session_id(),
            hand_no=self._hand_no,
            previous_state=previous,
            terminal_state=state,
            terminal_reason=reason,
            initiating_seat=seat,
            conn_id=conn_id,
            host_conn_id=self._host_conn_id,
            monotonic_ts=time.monotonic(),
            sequence=self._terminal_seq,
        )

        # Nothing queued may commit against a terminated session: a held
        # message replayed later, or a proof that finishes verifying after
        # the fact, would mutate state whose owner has already ended.
        self._invalidate_pending_work()

        # Callers may supply the sidecar_stopping payload so an existing
        # event contract survives being routed through here.
        self._safe_emit("sidecar_stopping",
                        **(event if event is not None
                           else {"reason": f"{state}: {reason}"}))
        if self.on_session_terminated:
            self.on_session_terminated(self.terminal_record)
        return True

    def _invalidate_pending_work(self) -> None:
        """Drop everything that could still mutate this session."""
        self._assert_owner()
        self._msg_buffer = []
        driver = self._deal_driver
        if driver is not None and getattr(driver, "deal", None) is not None:
            driver.deal._held.clear()
        self._clear_deadline()

    @owned
    def handle_disconnect(self, conn_id: str) -> None:
        """Called by the transport on_disconnect handler for any dropped peer.

        Host loss is phase-dependent. In LOBBY nothing cryptographic is in
        flight, so re-electing from already-authenticated membership is
        safe. Once PLAYING, host identity is frozen: a promotion there would
        hand host-only authority over an in-flight cryptographic protocol
        to a peer that inherited none of its state, with no authenticated
        transfer of that authority. So it terminates instead.
        """
        if self.terminal_state is not None:
            return                          # already terminal; late event

        with self._lock:
            self.players.pop(conn_id, None)
            if conn_id in self._join_order:
                self._join_order.remove(conn_id)

        if conn_id == self._host_conn_id:
            if self.state == "PLAYING":
                self.terminate(
                    self.HOST_LOST,
                    f"host connection {conn_id} dropped during play",
                    conn_id=conn_id)
                return
            # LOBBY only.
            self._elect_new_host()
        else:
            # A non-host peer dropped
            if self.is_host:
                self._broadcast_player_list()
            if self.on_player_list_changed:
                self.on_player_list_changed(list(self.players.values()))

    def _elect_new_host(self) -> None:
        """Lowest-join-order peer becomes the new host. LOBBY only.

        The guard is here, not only at the call site, on purpose: dormant
        migration code that another callback can reach is exactly the
        failure being removed. Any path that reaches this during PLAYING
        refuses rather than promoting.
        """
        self._assert_owner()
        if self.state != "LOBBY":
            _log.warning(
                "session: refusing host election in state %s — host "
                "identity is frozen once play begins", self.state)
            return
        if self.terminal_state is not None:
            return
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
                    "ed25519_pubkey_hex": p.ed25519_pubkey_hex,
                    "is_host":           p.is_host,
                    "ready":             p.ready,
                }
                for p in self.players.values()
            ]
            snapshot = list(self.players.values())
        self._transport.broadcast({"type": "player_list", "payload": {"players": players_data}})
        if self.on_player_list_changed:
            self.on_player_list_changed(snapshot)

    @owned
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
                ed25519_pubkey_hex = _id.public_key_bytes().hex(),
                is_host           = self.is_host,
                ready             = True,
            )
        self._safe_emit("peer_connected",
                         conn_id=conn_id, nickname=self.local_nickname)
        if self.is_host:
            self._broadcast_player_list()

    @owned
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

    @owned
    def start_game(self, table_settings: dict) -> None:
        """Host starts the game: broadcast game_start and transition to PLAYING."""
        if self.terminal_state is not None:
            raise RuntimeError(
                f"cannot start a game: session terminated "
                f"({self.terminal_state}: {self.terminal_reason})")
        if not self.is_host:
            raise RuntimeError("Only the host can start the game")
        with self._lock:
            seat_order = [p.conn_id for p in self.players.values()]
        self._seat_order = seat_order
        self._last_table_settings = table_settings
        self._prevention = bool(
            table_settings.get(self.PREVENTION_SETTING, False))
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

    @owned
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

    @owned
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

    # ------------------------------------------------------------------
    # Public configuration and decision API (Phase 2)
    # ------------------------------------------------------------------

    @property
    def seat_order(self) -> list[str]:
        """Read-only view of the current seat order (copy)."""
        return list(self._seat_order)

    @property
    def replica(self):
        """The ReplicaTable for the current hand, or None between hands."""
        return self._replica

    @owned
    def configure_seats(self, order: list[str]) -> None:
        """Set the seat order for the next (or only) hand.

        Validates:
        - 2–9 seats
        - All seat IDs are unique
        - The local conn_id (if known) appears exactly once
        - No active hand is in progress

        Raises:
            ValueError:  order fails structural validation
            RuntimeError: called during an active hand
        """
        if self._replica is not None and not self.hand_voided and self.hand_result is None:
            raise RuntimeError("cannot change seat order during an active hand")
        if len(order) < 2:
            raise ValueError(f"seat order needs at least 2 seats, got {len(order)}")
        if len(order) > 9:
            raise ValueError(f"seat order needs at most 9 seats, got {len(order)}")
        if len(set(order)) != len(order):
            raise ValueError("seat order contains duplicate conn_ids")
        if self.local_conn_id and self.local_conn_id not in order:
            raise ValueError(
                f"local conn_id {self.local_conn_id!r} not in seat order")
        self._seat_order = list(order)

    @owned
    def set_host_engine(self, engine) -> None:
        """Register the host-side engine for action routing (host only)."""
        self._engine = engine

    # ------------------------------------------------------------------
    # Timeout machinery (Phase 3)
    # ------------------------------------------------------------------

    @owned
    def check_deadlines(self) -> None:
        """Broadcast a timeout_proposal if the current deadline has expired.

        Call this periodically from the event loop (e.g. every second).
        Tests advance the FakeClock then call this directly — no sleeping.
        """
        if self._current_deadline_token is None or self._deadline_started_at is None:
            return
        token = self._current_deadline_token
        elapsed = self._clock.monotonic() - self._deadline_started_at
        limit   = self._phase_timeout.get(token.phase, 30.0)
        if elapsed >= limit:
            self._broadcast_timeout_proposal(token)

    def _broadcast_timeout_proposal(self, token: DeadlineToken) -> None:
        self._safe_emit(
            "timeout_proposed",
            hand        = self._hand_no,
            seq         = token.action_seq,
            phase       = self._replica.phase if self._replica else "unknown",
            actor       = token.actor,
            token_phase = token.phase,
        )
        self._transport.broadcast({
            "type":         "timeout_proposal",
            "hand":         self._hand_no,
            "token": {
                "hand_id":    token.hand_id,
                "phase":      token.phase,
                "actor":      token.actor,
                "action_seq": token.action_seq,
            },
            "missing_seat": None,
        })
        # The broadcaster is the first peer to act on its own proposal.
        # InMemoryBus (and real transports) exclude the sender from their
        # own broadcasts, so self-apply here to keep all replicas in sync.
        self._apply_timeout(token)

    @owned
    def _on_timeout_proposal(self, conn_id: str, msg: dict) -> None:
        """Receive and validate a timeout proposal; apply if it matches the
        current deadline and the action sequence still agrees."""
        if not self._hand_msg_ok(conn_id, msg):
            return
        if self._replica is None or self.hand_voided:
            return

        # Reconstruct the token from the wire.
        raw = msg.get("token", {})
        try:
            token = DeadlineToken(
                hand_id    = str(raw["hand_id"]),
                phase      = str(raw["phase"]),
                actor      = raw.get("actor"),          # may be None
                action_seq = int(raw["action_seq"]),
            )
        except (KeyError, TypeError, ValueError):
            _log.warning("session: malformed timeout_proposal from %s", conn_id)
            return

        # Reject if this proposal is not for the current deadline.
        if token != self._current_deadline_token:
            _log.debug("session: stale timeout_proposal from %s (token mismatch)",
                       conn_id)
            return

        # Reject if the replica has already advanced past the proposal's seq.
        if token.action_seq != self._replica.next_seq:
            _log.debug("session: out-of-order timeout_proposal from %s "
                       "(seq %d, expected %d)",
                       conn_id, token.action_seq, self._replica.next_seq)
            return

        self._apply_timeout(token)

    def _apply_timeout(self, token: DeadlineToken) -> None:
        """Apply the phase-specific consequence of an accepted timeout."""
        self._clear_deadline()
        self._safe_emit(
            "timeout_applied",
            hand        = self._hand_no,
            seq         = token.action_seq,
            phase       = self._replica.phase if self._replica else token.phase,
            digest      = self._replica.state_digest() if self._replica else "",
            actor       = token.actor,
            token_phase = token.phase,
        )
        if token.phase == "betting":
            self._apply_betting_timeout(token)
        elif token.phase in ("deal_shuffle", "deal_decrypt"):
            self._apply_deal_timeout(token)
        else:
            _log.warning("session: unhandled timeout phase %r", token.phase)

    def _apply_betting_timeout(self, token: DeadlineToken) -> None:
        """Fold (if facing a bet) or check (if not) on behalf of the timed-out actor.

        The action goes through the normal apply_action path so digest
        checking and _pump_hand stay on the single happy path.
        """
        if self._replica is None or self.hand_voided:
            return
        try:
            seat = self._seat_order.index(token.actor)
        except ValueError:
            _log.warning("session: betting timeout actor %r not in seat order",
                         token.actor)
            return
        legal  = self._replica.engine.legal(seat)
        action = "fold" if legal.get("to_call", 0) > 0 else "check"
        verdict = self._replica.apply_action(token.action_seq, seat, action, 0)
        if verdict == "applied":
            self._pump_hand()
            self._notify_state_changed()

    @owned
    def _apply_deal_timeout(self, token: DeadlineToken) -> None:
        """Void the hand when a deal-phase contribution never arrived.

        Stacks are preserved (existing void-and-redeal path). The missing
        peer (if identifiable) is marked unavailable; it is NOT removed.
        """
        if self._replica is None or self.hand_voided:
            return
        actor = token.actor
        reason = f"deal timeout: {token.phase}"
        if actor is not None:
            reason += f" (peer {actor!r} did not contribute)"
            with self._lock:
                if actor in self.players:
                    self.players[actor].unavailable = True
            self._safe_emit("peer_unavailable", conn_id=actor)
        self._void_hand(reason)

    @owned
    def _maybe_start_deadline(self) -> None:
        """Evaluate whether a deadline needs to be started or cleared after
        any state change.  Called automatically by _notify_state_changed."""
        # No deadline when no active, non-settled hand exists.
        if self._replica is None or self.hand_voided or self.hand_result is not None:
            self._clear_deadline()
            return

        # If the deal is still in flight, track a deal deadline regardless of
        # the replica's betting phase (betting cannot proceed without holes).
        if (self._deal_driver is not None
                and not self.deal_done()
                and not self.deal_aborted()):
            token = DeadlineToken(
                hand_id    = self._deal_session_id(),
                phase      = "deal_shuffle",
                actor      = None,
                action_seq = self._replica.next_seq,
            )
            if token != self._current_deadline_token:
                self._start_deadline(token)
            return

        # Deal is done (or absent). Track the current actor's betting turn.
        from holdem.p2p.replica_table import PHASE_BETTING
        if self._replica.phase != PHASE_BETTING:
            self._clear_deadline()
            return

        actor = self._replica.actor
        if actor is None or not (0 <= actor < len(self._seat_order)):
            self._clear_deadline()
            return

        actor_conn = self._seat_order[actor]
        if actor_conn == self.local_conn_id:
            # It is our turn; we set no deadline on ourselves.
            self._clear_deadline()
            return

        token = DeadlineToken(
            hand_id    = self._deal_session_id(),
            phase      = "betting",
            actor      = actor_conn,
            action_seq = self._replica.next_seq,
        )
        if token != self._current_deadline_token:
            self._start_deadline(token)

    def _start_deadline(self, token: DeadlineToken) -> None:
        self._current_deadline_token = token
        self._deadline_started_at   = self._clock.monotonic()

    def _clear_deadline(self) -> None:
        self._current_deadline_token = None
        self._deadline_started_at   = None
