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
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from holdem.p2p.timeout import (
    Clock, DeadlineToken, RealClock, DEFAULT_PHASE_TIMEOUTS,
)
from holdem.p2p.admission import ADMISSION_TYPES as _ADMISSION_TYPES
from holdem.p2p.events import EventSink, NullSink, SCHEMA_VERSION

_log = logging.getLogger(__name__)

# First author_seq every (hand, seat) stream uses. Fixed here rather than
# left to each send path to invent, so a receiver knows what "no messages
# yet" means without guessing.
AUTHOR_SEQ_START = 0

# Authorization modes for the peer-authored, host-relayed path.
#
# The mode used to be implicit: _seat_author_ok fell back to "the delivering
# connection owns the seat" whenever no seat keys were bound. That reads as a
# harmless default and is not one -- it is the difference between a protocol
# that authenticates authors and one that trusts whoever handed it the bytes,
# chosen silently by whether an unrelated initialisation step had run. Control
# N on PR #31 showed the consequence: with bindings absent the host relayed
# a joiner's traffic permissively and only the recipient refused it.
#
# The mode is now decided once, at construction, from an explicit declaration
# by the transport, and is readable as ``session.author_mode``.
AUTHOR_MODE_WIRE   = "wire"     # verified envelopes; seat bindings REQUIRED
AUTHOR_MODE_COMPAT = "compat"   # unsigned flat dicts; conn_id stands in

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
    #: The deal context this session was running under, or None if it
    #: died before one existed (e.g. terminated from the lobby).
    session_id: Optional[str]
    hand_no: int
    previous_state: str
    terminal_state: str
    terminal_reason: str
    initiating_seat: Optional[int]
    conn_id: Optional[str]
    host_conn_id: str
    monotonic_ts: float
    sequence: int
    #: The table's deal policy, recorded because session_id no longer
    #: reveals it. session_id used to be a readable "poker|a|b|c"; it is now
    #: a digest of a canonical context, which is the right thing for a
    #: domain separator and useless to a human reading an incident. Carried
    #: explicitly so a POLICY_REFUSED record says what was refused.
    deal_policy: Optional[str] = None


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


def _is_seat(value) -> bool:
    """A usable seat index: a real int, never a bool.

    One definition, because ingress and the handlers must agree exactly on
    what counts. They did not before: ingress required isinstance(int) while
    some handlers coerced with int(), so a seat of "1" was declined by the
    authorizing path and then revived by the applying one. ``True == 1`` in
    Python, so bool is excluded here rather than at four call sites.
    """
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class HostlessInbound:
    """One inbound peer-authored message, with its three identities separated.

    The protocol keeps three different notions of "who sent this" and the
    #30 defect came from letting them blur together inside a dict:

      conn_id  -- the transport hop. Who handed us these bytes. Under the
                  star relay this is the HOST for anything a joiner
                  authored, so it is not an authorization input.
      author   -- the Ed25519 public key that signed the envelope. The
                  protocol author, and the only one of the three a peer
                  cannot assert about itself.
      seat     -- the gameplay identity the payload claims. Authorized by
                  checking it against the key bound to that seat.

    ``_hostless_body`` used to smuggle the author back into the payload
    dict under "pubkey", which callers had to remember; forgetting it made
    every remote message unauthorizable while the in-memory suite stayed
    green. Here it is a named field, so dropping it is a type error's worth
    of obvious rather than an invisible one.
    """

    mtype:       str
    body:        dict            # normalized flat form the state machines use
    conn_id:     str             # transport hop
    author:      Optional[str]   # Ed25519 pubkey hex, or None if unsigned
    seat:        Optional[int]   # claimed gameplay identity
    hand:        Optional[int]
    fingerprint: str             # identity of the signed thing
    verified:    bool            # arrived inside a verified wire envelope
    local:       bool            # self-delivery: no wire round-trip
    envelope:    dict            # original, forwarded unchanged when relaying


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

    #: Key under table_settings naming the table-wide deal policy, and the
    #: only two values that name one. There is deliberately no default: a
    #: table that does not say how it deals is refused, because the previous
    #: arrangement -- absent-or-false means detection-only -- is exactly how
    #: every shipped game came to run without shuffle proofs while the
    #: prevention implementation sat complete and unreferenced.
    DEAL_POLICY_SETTING   = "deal_policy"
    DEAL_POLICY_BG        = "bayer-groth-v1"
    DEAL_POLICY_DETECTION = "detection-only-v1"
    DEAL_POLICIES = frozenset({DEAL_POLICY_BG, DEAL_POLICY_DETECTION})

    #: Terminal outcome for a table this peer will not play on.
    POLICY_REFUSED = "POLICY_REFUSED"

    @classmethod
    def parse_deal_policy(cls, table_settings, author_mode) -> Optional[str]:
        """Return the table's deal policy, or None if it does not have one.

        Strict, and deliberately total: it NEVER raises. Two of the three
        call sites are inbound message handlers, where raising would carry a
        hostile or stale game_start out of handle_message and onto the
        transport's dispatch thread. Refusal is a value here; the host path
        turns that value into an exception itself.

        Rejects, in order: a non-dict settings blob, a non-str value (which
        is what catches True/False/1/0/None -- note bool is an int but not a
        str, so the isinstance check is the thing that stops the old
        coercion from creeping back), an unrecognised string, and finally
        anything but Bayer-Groth on a verified-envelope transport.

        That last rule is the mandate: detection-only remains a legitimate,
        explicitly-declared mode for compat, test and benchmark tables,
        where the deal is not carrying real money between strangers. On the
        wire it is refused, because a table whose security property is "we
        will notice afterwards that someone cheated" is not one this
        protocol is willing to advertise as trustless.
        """
        if not isinstance(table_settings, dict):
            return None
        value = table_settings.get(cls.DEAL_POLICY_SETTING)
        if not isinstance(value, str):
            return None
        if value not in cls.DEAL_POLICIES:
            return None
        if author_mode == AUTHOR_MODE_WIRE and value != cls.DEAL_POLICY_BG:
            return None
        return value

    def __init__(self, is_host: bool, nickname: str, avatar_b64: str,
                 transport=None, clock: Optional[Clock] = None,
                 sink: Optional[EventSink] = None,
                 master_secret: Optional[bytes] = None,
                 author_mode: Optional[str] = None,
                 admission=None, joiner_admission=None):
        # The one serialized execution context for this session's
        # protocol state. Created first: every field below is only ever
        # mutated while this is held.
        self._owner = SessionOwner()
        self.is_host    = is_host
        # require_prevention used to live here: a per-peer flag meaning
        # "refuse a table that is not running prevention". It is gone
        # because deal_policy subsumes it and the two together were
        # ambiguous -- a table declaring detection-only while a peer
        # required prevention left no single answer to "what is this table
        # running?". Wire mode now mandates Bayer-Groth for everyone, so
        # there is nothing left for a per-peer override to express.
        #
        # It cost one capability, and the loss is real rather than
        # theoretical: a COMPAT peer can no longer demand prevention, since
        # only wire mode forbids detection-only. That is acceptable because
        # compat is harnesses and benchmarks, but it is a subtraction, not
        # a refactor.
        # transport module (or a mock) providing broadcast()/send().
        # Defaults to the real global transport; tests inject an
        # in-memory one so N sessions can run in one process.
        if transport is None:
            from holdem.p2p import transport as _t_module
            transport = _t_module
        self._transport = transport

        # Which authorization rule this session runs under, decided ONCE and
        # readable afterwards. There is no default: a transport must declare
        # whether what it delivers has been signature-verified, or the caller
        # must state the mode outright.
        #
        # Silence is an ERROR rather than compatibility mode, and that is the
        # point of the whole arrangement. Treating an undeclared transport as
        # compat would be the same implicit downgrade this refactor removed
        # from _seat_author_ok, merely relocated into capability detection:
        # add a transport, forget one attribute, and the session quietly
        # concludes "no declaration -> compat -> trust the delivering
        # connection". A missing declaration is not evidence that conn_id
        # trust is safe; it is evidence that nobody has said.
        if author_mode is None:
            declared = getattr(transport, "delivers_verified_envelopes", None)
            if not isinstance(declared, bool):
                raise TypeError(
                    f"{getattr(transport, '__name__', type(transport).__name__)} "
                    "does not declare delivers_verified_envelopes (bool). A "
                    "transport must state whether what it delivers has been "
                    "signature-verified, because that decides whether seat "
                    "authority comes from the signing key or from the "
                    "delivering connection. Set the attribute on the "
                    "transport, or pass author_mode= explicitly.")
            author_mode = (AUTHOR_MODE_WIRE if declared
                           else AUTHOR_MODE_COMPAT)
        if author_mode not in (AUTHOR_MODE_WIRE, AUTHOR_MODE_COMPAT):
            raise ValueError(f"unknown author_mode {author_mode!r}")
        self.author_mode = author_mode

        # Admission policy (M-8). A host on the production transport MUST
        # have one: without it the lobby gate is open and any process that
        # can sign an envelope becomes a Player. Requiring it here rather
        # than trusting call sites means the insecure configuration cannot
        # be reached by omission -- the same discipline as author_mode.
        #
        # Compat harnesses may omit it. That is not a production bypass:
        # AUTHOR_MODE_COMPAT is only selected by a transport that declares
        # it delivers no verified envelopes, and the production transport
        # declares the opposite.
        # KNOWN GAP, deliberately not closed here. A wire-mode JOINER built
        # without a pin has _pinned_host_pubkey = None, so the roster check
        # in _on_player_list silently no-ops and the session accepts
        # whatever host key it is told. Independent review classed this as
        # structural rather than live: onboarding always supplies a pin and
        # sidecar_launcher runs in compat, so no shipped path reaches it.
        #
        # Requiring it here is the obvious fix and was tried; it fails 34
        # existing tests whose doubles build wire-mode joiners for reasons
        # unrelated to admission. That is a real change to the test estate,
        # not a bug fix, and it does not belong in a correction pass. Left
        # as a named gap rather than a silent one.
        if admission is None and is_host and author_mode == AUTHOR_MODE_WIRE:
            raise ValueError(
                "a host on a verified-envelope transport must be given an "
                "admission policy (holdem.p2p.admission.HostAdmission); "
                "without one, any connection that can sign an envelope "
                "would be able to join the lobby")
        self._admission = admission
        # Joiner side. When set, this session refuses everything but the
        # admission handshake until a peer has authenticated as the exact
        # host key the invite pinned. _host_authenticated is the switch;
        # nothing but a verified admission_accept may flip it.
        self._joiner_admission = joiner_admission
        self._host_authenticated = joiner_admission is None
        #: Set on successful host authentication; the exact 32-byte key the
        #: invite pinned, kept to close the chain when the roster arrives.
        self._pinned_host_pubkey = None

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

        # Table-wide deal policy. None until a table is accepted, and then
        # write-once through _adopt_deal_policy -- never assigned directly,
        # by any path, including the local host's own start_game. Held
        # separately from _last_table_settings because that dict is only
        # overwritten when non-empty, which would let a stale policy survive
        # into a table running under a different one.
        self._deal_policy: Optional[str] = None
        # Last successfully-built deal context id. Terminal recording
        # reads this instead of recomputing, so a session can always
        # produce a record even when the context no longer encodes.
        self._deal_context_id: Optional[str] = None

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
        # Author sequencing, keyed (hand, seat). _out is the next number we
        # will stamp; _seen maps each number received from that author to the
        # fingerprint of the envelope it arrived in.
        #
        # A mapping rather than a set, and not for bookkeeping tidiness: a
        # set can only answer "was this number used", which makes a second
        # message under the same number a duplicate. Two DIFFERENT validly
        # signed envelopes at one number is equivocation, and collapsing
        # that into "duplicate" would drop the second quietly and leave two
        # peers on different states with nothing recorded. The hash is what
        # separates the harmless case from the hostile one.
        self._author_seq_out: dict[tuple, int] = {}
        self._author_seq_seen: dict[tuple, dict] = {}

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

        # ---- admission gate (M-8) ------------------------------------
        # The host answers NOTHING but the admission handshake until a
        # connection has proved it holds the invite. Before this existed,
        # _on_player_info created a Player from any correctly-signed
        # connection -- and "correctly signed" only means the sender owns
        # some Ed25519 key, which anyone can generate. Possession of the
        # room code was never demonstrated to the host at all.
        #
        # Placed ahead of the hash-chain bookkeeping deliberately. That
        # code writes _peer_last_hash[conn_id] for every message it sees,
        # so gating after it would let an unadmitted connection seed
        # per-peer chain state -- small, but it is state, written on behalf
        # of a peer that has not yet earned the right to any.
        if not self._admission_ok(conn_id, msg.get("type"),
                                  msg.get("pubkey")):
            return

        # The handshake itself is answered HERE, by the Session that owns
        # the perimeter and the policy -- not by a caller.
        #
        # It was originally left to the application layer, and the joiner
        # half was implemented in onboarding while the host half existed
        # only in a test harness. The result passed every test and could
        # not complete a single real handshake: a production host received
        # admission_hello and replied with nothing, so no connection could
        # ever be admitted and the perimeter held vacuously. An independent
        # review found it; 1249 green tests did not.
        #
        # Ownership is the fix, not another branch. Session already holds
        # the HostAdmission policy and already decides what an unadmitted
        # connection may say, so it is the only place where "may you speak"
        # and "here is how you earn that" cannot drift apart.
        #
        # Answered before the hash-chain bookkeeping for the same reason
        # the gate precedes it: a peer mid-handshake has not yet earned any
        # per-peer state.
        if msg.get("type") in _ADMISSION_TYPES:
            self._answer_admission(conn_id, msg)
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
        body = msg
        if t in _HOSTLESS_PAYLOAD_TYPES:
            # The peer-authored, host-relayed ingress pipeline. Every step
            # runs exactly once, in this order, for all eight types:
            #
            #   normalize -> author/seat -> authorize -> sequence -> relay
            #
            # then the typed handler below applies hand scope and gameplay.
            # These responsibilities used to be spread across handle_message,
            # _hostless_body, _hostless_seq_ok, _maybe_relay and each
            # handler, with authorization repeated in three of them. That was
            # correct by repetition rather than by structure: nothing stopped
            # the copies drifting, and each copy had to re-derive the author
            # from a dict that only conventionally still carried it.
            ctx = self._normalize_hostless(conn_id, msg)
            if not self._admit_hostless(ctx):
                return
            body = ctx.body
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
    def _hostless_projection(msg: dict):
        """(body, author, enveloped) -- the pure representation rule.

        The single definition of how the two shapes a hostless message can
        arrive in map onto the flat form the state machines use. Session
        state is not consulted, so both the instance path
        (_normalize_hostless) and the static view (_hostless_body) share it
        rather than each reimplementing the unwrap.
        """
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            body = dict(msg)
            author = body.get("pubkey")
            return body, (author if isinstance(author, str) else None), False
        body = dict(payload)
        body["type"] = msg.get("type")
        # The author lives on the ENVELOPE. A payload-supplied "pubkey" must
        # never win -- only the envelope's was verified by wire.unpack -- so
        # the payload's copy is overwritten or removed, never merged.
        author = msg.get("pubkey")
        if isinstance(author, str):
            body["pubkey"] = author
        else:
            author = None
            body.pop("pubkey", None)
        return body, author, True

    @staticmethod
    def _hostless_body(msg: dict) -> dict:
        """The flat projection of a hostless message, for the state machines.

        A view over _hostless_projection, which is the representation rule.
        Kept static and with this signature because the adversarial suites
        call it directly (some unbound, on the class) to assert what
        survives the unwrap -- notably that the ENVELOPE's author wins over
        any pubkey the payload tries to supply.

        On the ingress path nothing relies on the author riding inside this
        dict any more: HostlessInbound carries it as a named field. That
        implicit contract -- "remember this dict still has the pubkey on it"
        -- is what the #30 defect broke, silently, while the in-memory suite
        stayed green because it takes the compat rule and never looks.
        """
        return Session._hostless_projection(msg)[0]

    # ------------------------------------------------------------------
    # Mental-poker deal (L5) — hostless, peer-symmetric. Each peer drives
    # its own MentalDealDriver for the local seat; messages carry seat
    # indices and are self-describing, so routing is by seat, not conn_id.
    # ------------------------------------------------------------------

    #: Domain label for the deal context pre-image. Bump with the LAYOUT,
    #: not with the product: it exists so two different encodings can never
    #: produce the same digest.
    _DEAL_CTX_LABEL   = b"poker.deal.context"
    _DEAL_CTX_VERSION = 2

    def _deal_context_bytes(self) -> bytes:
        """Injective encoding of (layout version, deal policy, seat order).

        Every variable-length field is length-prefixed, so no two distinct
        inputs share an encoding. The old form -- "poker|" + "|".join(order)
        -- was not injective: ["a|b", "c"] and ["a", "b|c"] both encode to
        "poker|a|b|c", so two structurally different tables shared a DKG
        domain, and a proof-of-possession minted at one verified at the
        other. Not reachable on the honest path, where conn_ids are UUIDs,
        but _on_game_start adopts whatever seat_order a host sends without
        validating its contents, so a malicious host could reach it.

        The version is encoded INSIDE the pre-image, not merely prefixed
        onto the digest, so a future layout cannot collide with this one.

        Non-str seat ids raise, matching the old behaviour where '|'.join
        raised on them. Coercing with str() would trade a loud failure for
        an injectivity hole, which is the opposite of the point.

        Refuses to encode a session with no adopted policy, rather than
        substituting a placeholder. An earlier version used
        `self._deal_policy or ""`, which folded None and "" onto the same
        pre-image and quietly broke the injectivity this whole function
        exists for. The encoding has no representation for an invalid
        lifecycle state because it should never be asked to describe one:
        a deal context is meaningful only once a table is accepted, and
        forensic callers must not route through here at all.
        """
        if self._deal_policy is None:
            raise RuntimeError(
                "cannot build a deal context before a policy is adopted; "
                "forensic callers want _recorded_session_id()")
        out = bytearray()
        out += len(self._DEAL_CTX_LABEL).to_bytes(4, "big")
        out += self._DEAL_CTX_LABEL
        out += self._DEAL_CTX_VERSION.to_bytes(4, "big")
        policy = self._deal_policy.encode("utf-8")
        out += len(policy).to_bytes(4, "big") + policy
        out += len(self._seat_order).to_bytes(4, "big")
        for cid in self._seat_order:
            if not isinstance(cid, str):
                raise TypeError(
                    f"seat id must be str, got {type(cid).__name__}: {cid!r}")
            raw = cid.encode("utf-8")
            out += len(raw).to_bytes(4, "big") + raw
        return bytes(out)

    def _deal_session_id(self) -> str:
        """Shared, stable per-game id: a digest of the canonical context.

        Stays a str -- MentalDeal encodes it, it becomes DeadlineToken's
        hand_id and is JSON-serialised onto the wire, and it is interpolated
        into abort messages. Nothing anywhere parses it.

        Binding the deal policy in makes "bayer-groth-v1" a real context
        commitment rather than a label travelling alongside the deal: two
        peers running different policies now derive different DKG domains
        and cannot complete a hand together at all.

        The cost is attribution, and it is worth naming. A policy mismatch
        now surfaces at DKG as a proof-of-possession failure, which blames
        an honest peer, rather than later as a missing-proof abort naming
        the shuffler. Under the mandate a mismatch requires an equivocating
        host -- wire mode admits exactly one policy, and every peer reads it
        from the same message -- so the trade is a legible-but-misattributed
        failure in exchange for making the commitment real. The terminal
        record carries the policy so the cause is recoverable.

        Caches on success so that terminal recording never has to recompute
        it. See _recorded_session_id.
        """
        session_id = (f"poker.deal.v{self._DEAL_CTX_VERSION}:"
                      + hashlib.sha256(self._deal_context_bytes()).hexdigest())
        self._deal_context_id = session_id
        return session_id

    def _recorded_session_id(self) -> Optional[str]:
        """The deal context for forensics. NEVER raises, never computes.

        terminate() must be atomic: it sets the terminal flags and then
        builds the record, so anything fallible between those two points
        leaves a session that is terminal but never produced a record,
        never invalidated its pending work, and never told its callbacks.
        Terminal state is the strongest lifecycle invariant here, and it
        cannot depend on encoding something that may not exist.

        It very nearly did. _deal_context_bytes raises on a non-str seat
        id, _on_game_start adopts whatever seat_order a host sends without
        validating its shape, and terminate() called straight through to
        it -- so a malicious host could send seat_order ["host", 7, "me"]
        and split the terminal transition in half.

        Returns None when no deal context was ever built, which is the
        honest answer for a session that died in the lobby.
        """
        return self._deal_context_id

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
    def deal_policy(self) -> Optional[str]:
        """The accepted table deal policy, or None before a table is joined."""
        return self._deal_policy

    @property
    def proofs_verified(self) -> int:
        """Shuffle proofs this seat has verified in the current hand.

        The difference between a table that declares Bayer-Groth and one
        that runs it. deal_policy is what the table SAYS; this is what
        actually happened, so a downgrade that leaves the policy string
        intact -- the whole failure mode this mandate exists to prevent --
        shows up here as zero.

        Scope, stated precisely because an earlier version of this
        docstring overclaimed: it counts verifications that ran and
        returned true. It does not attest that the verifier is sound, and
        a bg_shuffle.verify hardwired to return true would increment it
        normally. That is the BG soundness suites' property, not this
        counter's.
        """
        driver = self._deal_driver
        if driver is None or driver.deal is None:
            return 0
        return driver.deal._proofs_verified

    @property
    def prevention(self) -> bool:
        """Whether this table runs Bayer-Groth shuffle proofs.

        The single mapping point from the policy string to the boolean the
        deal layer consumes. MentalDeal and MentalDealDriver still key off a
        bool, and that is fine, but the translation must happen exactly
        once: a second place deriving the same bool is a place the two can
        drift, and drift here means one peer producing a proof another does
        not expect.

        Table-wide and uniform by construction: the policy rides in the same
        game_start every peer already receives, so peers do not negotiate.
        A peer that disagrees produces or expects a proof the others do not,
        and the hand fails closed rather than silently dropping to
        detection-only.
        """
        return self._deal_policy == self.DEAL_POLICY_BG

    def _adopt_deal_policy(self, policy: str) -> bool:
        """The ONE writer of _deal_policy. True if the session now holds it.

        An explicit three-state machine, because the interesting case is the
        third one:

          None -> valid    adopted
          same -> same     idempotent; retries and relay echoes are harmless
          A    -> B        REFUSED, and the caller must terminate

        Every path goes through here, including the host's own start_game.
        The temptation is to let the local host assign directly on the
        grounds that it is the one proposing the table -- but a field whose
        invariant holds only for callers who remembered it is the defect
        pattern this codebase has now paid for repeatedly. One writer.

        It validates its own input rather than trusting callers to have
        parsed first. A single writer that accepts anything is only half a
        chokepoint: it centralises WHEN the field changes while leaving WHAT
        it may hold to whoever calls it. Review found `_adopt_deal_policy
        ("banana")` succeeded, reachable from _deal_first_hand, which takes
        a caller-supplied policy. An unknown value is a programming error,
        not a protocol event, so it raises rather than returning False --
        the False channel means "refused a legitimate change".
        """
        if policy not in self.DEAL_POLICIES:
            raise ValueError(
                f"not a deal policy: {policy!r}; "
                f"expected one of {sorted(self.DEAL_POLICIES)}")
        current = self._deal_policy
        if current is None:
            self._deal_policy = policy
            return True
        return current == policy

    def _assert_deal_preconditions(self) -> None:
        """Everything that must hold before a hand may exist at all.

        Shared by _begin_p2p_hand (which calls it before constructing any
        gameplay state) and begin_hand (which re-checks at the last moment
        before the driver exists). Two callers, one rule: a check duplicated
        by hand is a check that can drift.
        """
        if self.terminal_state is not None:
            raise RuntimeError(
                f"cannot begin a hand: session terminated "
                f"({self.terminal_state}: {self.terminal_reason})")
        if self._deal_policy is None:
            raise RuntimeError(
                "cannot begin hand: no deal policy has been adopted; a hand "
                "must follow an accepted table")
        if self.author_mode == AUTHOR_MODE_WIRE and not self.prevention:
            raise RuntimeError(
                f"cannot begin hand: wire mode requires "
                f"{self.DEAL_POLICY_BG!r}, but the adopted policy is "
                f"{self._deal_policy!r}")

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
        # Terminality, policy adoption and the wire-mode mandate. Checked
        # here as well as in _begin_p2p_hand, at the last point before a
        # driver exists: everything above this line is policy, everything
        # below deals cards. begin_hand is also called directly by tests and
        # harnesses that never go through _begin_p2p_hand.
        self._assert_deal_preconditions()
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

    def _send_hostless(self, m: dict) -> None:
        """The ONE place a local hostless message leaves this peer.

        Eight types are broadcast from five call sites. Stamping author
        identity and sequence at each would let them drift, and a type
        that forgot to stamp would be indistinguishable, to a receiver,
        from one that had been suppressed.

        Stamped BEFORE the transport signs, so (hand, seat, author_seq) is
        covered by the Ed25519 signature and a relaying host cannot
        renumber a message without invalidating it.

        ``seat`` is filled in when absent. timeout_proposal carried none at
        all, so it could not be attributed, could not be sequenced, and --
        since _maybe_relay keys on the claimed seat -- was never actually
        relayed despite being in the hostless set.
        """
        seat = m.get("seat", m.get("seat_from"))
        if not isinstance(seat, int) or isinstance(seat, bool):
            try:
                seat = self.local_seat
            except ValueError:
                self._transport.broadcast(m)      # unseated; nothing to stamp
                return
            m["seat"] = seat
        key = (int(m.get("hand", self._hand_no)), seat)
        nxt = self._author_seq_out.get(key, AUTHOR_SEQ_START)
        m["author_seq"] = nxt
        self._author_seq_out[key] = nxt + 1
        self._transport.broadcast(m)

    def _author_seq_ok(self, seat: int, hand: int, msg: dict,
                       fingerprint: str) -> bool:
        """Record one message in its author's stream. False means drop.

        Authorship is checked by the caller FIRST: a sequence number only
        means something once we know who is claiming it, or a stranger
        could desynchronise a real seat's stream.

        NOT contiguity-enforcing, and that is deliberate. An earlier
        revision demanded the next expected number and voided the hand on
        anything ahead of it. That contradicts a property this protocol is
        built to have and has tests for: delivery may reorder, so seq 1
        legitimately arrives before seq 0 and the replicas are still
        required to converge (tests/test_convergence_chaos.py). Voiding
        there turned honest reordering into a dead hand -- the enforcement
        was detecting the network, not an attacker.

        What the stream does give:

        * duplicates are dropped -- applied at most once per (hand, seat,
          author_seq), which is what stops a relay replaying traffic back
          at the table, and preserves idempotence under a bus that may
          deliver the same message twice;
        * numbers are signed, so a relay cannot renumber or strip one
          without invalidating the envelope (see _send_hostless);
        * a hole is observable via author_seq_holes() rather than acted on
          the instant it appears, because in-flight reordering and
          suppression look identical until the stream stops.

        Acting on a hole is the timeout machinery's job, which is also the
        only thing that can catch TOTAL suppression -- where nothing
        arrives and there is no gap to see.
        """
        got = msg.get("author_seq")
        if not isinstance(got, int) or isinstance(got, bool):
            return True              # unsequenced peer/harness: nothing to check
        key = (hand, seat)
        seen = self._author_seq_seen.setdefault(key, {})
        prior = seen.get(got)
        if prior is None:
            seen[got] = fingerprint
            return True
        if prior == fingerprint:
            _log.debug("session: seat %s re-sent author_seq %d in hand %s "
                       "-- identical envelope, already applied, dropping",
                       seat, got, hand)
            return False
        # Same number, two DIFFERENT validly signed envelopes. Not a
        # duplicate: the author said two things under one sequence number,
        # which is equivocation and the reason this map stores a hash rather
        # than merely remembering the number was used. A set would have
        # called the second one a duplicate and dropped it quietly, leaving
        # two peers on different states with nothing recorded.
        _log.warning("session: seat %s equivocated at author_seq %d in hand "
                     "%s: %s vs %s", seat, got, hand, prior[:16],
                     fingerprint[:16])
        self._void_hand(
            f"seat {seat} signed two different messages at author_seq {got} "
            f"(envelopes {prior[:16]} and {fingerprint[:16]})",
            outcome="VOID_EQUIVOCATION", blamed_seat=seat)
        return False

    @staticmethod
    def _envelope_fingerprint(envelope: dict, body: dict) -> str:
        """Identity of the signed thing this message came from.

        The envelope hash is authoritative on the production path: it covers
        every signed field, so two different messages from one author cannot
        collide and a relay cannot make two different messages look alike.

        The in-memory harness carries no envelope, so the body is hashed
        canonically instead. That keeps the equivocation check meaningful
        there -- same content hashes the same, different content does not --
        without pretending an unsigned message is signed.
        """
        h = envelope.get("hash") if isinstance(envelope, dict) else None
        if isinstance(h, str) and h:
            return h
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                               default=repr).encode("utf-8", "replace")
        return hashlib.sha256(canonical).hexdigest()

    def author_seq_holes(self, hand: int, seat: int) -> list:
        """Numbers missing below the highest one seen from (hand, seat).

        Empty while a stream is merely out of order but complete. Non-empty
        means something between that author and us has not arrived -- which
        during a hand may still be reordering in flight, and is only
        evidence once a deadline says waiting has become failure.

        A late message filling a hole closes it, so this is re-read at the
        moment it is needed rather than latched when a gap first appears.

        Blind spot, stated because it bounds what this can prove: if the
        TAIL of a stream is suppressed there is no higher number to reveal
        the absence, and this returns []. Nothing here can see a message
        whose existence was never observed; the phase deadline catches that
        as missing progress instead.
        """
        seen = self._author_seq_seen.get((hand, seat))
        if not seen:
            return []
        return [n for n in range(AUTHOR_SEQ_START, max(seen)) if n not in seen]

    def author_seq_hole_report(self, hand: Optional[int] = None) -> list:
        """Every unresolved hole in a hand, as [{seat, missing}] records.

        Snapshotted by the timeout path so a liveness failure can say what
        it observed missing. Empty when every stream is whole.
        """
        h = self._hand_no if hand is None else hand
        out = []
        for (khand, seat) in sorted(self._author_seq_seen):
            if khand != h:
                continue
            missing = self.author_seq_holes(khand, seat)
            if missing:
                out.append({"seat": seat, "missing": missing})
        return out

    def mark_host_authenticated(self, conn_id: str,
                                host_pubkey=None) -> bool:
        """The joiner's host hop, established the only way it may be.

        Called after a signed admission_accept has verified against the
        exact 32-byte key the invite pinned. Until this runs, the session
        drops everything but the handshake, so an endpoint that merely
        answered the socket cannot pass itself off as the host by speaking
        first.

        Deliberately explicit rather than inferred from message flow: the
        old inference -- "whoever sent the first player_ack" -- is the bug
        this replaces, and an implicit rule is what let it hide.

        ``host_pubkey`` is the key the CALLER believes it authenticated. It
        is checked against the pin rather than trusted, so a call site that
        drifts -- passing the envelope's author instead of the invite's, say
        -- fails closed here instead of quietly opening the session to
        whatever it just verified against itself. Returns whether the hop
        was established.
        """
        if self._joiner_admission is None:
            return False
        pinned = self._joiner_admission.host_pubkey
        if host_pubkey is not None:
            if bytes(host_pubkey) != bytes(pinned):
                _log.warning(
                    "session: refusing to authenticate %s -- caller offered "
                    "%s but the invite pinned %s", conn_id,
                    bytes(host_pubkey).hex()[:16], bytes(pinned).hex()[:16])
                return False
        self._host_conn_id = conn_id
        self._host_authenticated = True
        # Retained so the chain can be closed later: the roster the host
        # sends must name THIS key for the host's own seat. Without it the
        # joiner's seat keys are simply whatever the host asserted, and
        # "invite key == frozen seat key" is a claim nothing checks.
        self._pinned_host_pubkey = bytes(pinned).hex()
        return True

    @staticmethod
    def _adm_bytes(value):
        """Hex -> bytes, or None. Never raises on attacker input."""
        if not isinstance(value, str):
            return None
        try:
            return bytes.fromhex(value)
        except ValueError:
            return None

    def _answer_admission(self, conn_id: str, msg: dict) -> None:
        """Host side of hello -> challenge -> response -> accept.

        Non-hosts drop these: a joiner's handshake is driven by
        JoinAuthenticator, which intercepts admission traffic before the
        Session sees it, so anything reaching here on a joiner is a peer
        trying to talk protocol at the wrong end.

        Every field is parsed defensively. These are the only messages an
        UNADMITTED connection can send, which makes them the one attacker-
        reachable surface ahead of the perimeter; a malformed nonce must be
        a dropped message, never an exception on the ingress path.
        """
        if not self.is_host or self._admission is None:
            return
        mtype = msg.get("type")
        payload = msg.get("payload")
        body = payload if isinstance(payload, dict) else msg
        author = self._adm_bytes(msg.get("pubkey"))
        if author is None or len(author) != 32:
            return

        if mtype == "admission_hello":
            nonce = self._adm_bytes(body.get("client_nonce"))
            if nonce is None:
                return
            challenge = self._admission.on_hello(conn_id, author, nonce)
            if challenge is None:
                return
            self._transport.send(conn_id,
                                 {"type": "admission_challenge", **challenge})

        elif mtype == "admission_response":
            client_nonce = self._adm_bytes(body.get("client_nonce"))
            server_nonce = self._adm_bytes(body.get("server_nonce"))
            mac = self._adm_bytes(body.get("mac"))
            if client_nonce is None or server_nonce is None or mac is None:
                return
            if not self._admission.on_response(conn_id, author, client_nonce,
                                               server_nonce, mac):
                _log.warning("session: admission refused for %s", conn_id)
                return
            accept = self._admission.accept_payload(conn_id)
            if accept is None:                  # cannot happen; fail closed
                return
            _log.info("session: admitted %s as %s", conn_id,
                      author.hex()[:16])
            self._transport.send(conn_id,
                                 {"type": "admission_accept", **accept})

    def _admission_ok(self, conn_id: str, mtype, msg_pubkey=None) -> bool:
        """May this connection say this yet? Host-side capability gate.

        Three cases, and only the first is a gate:

        * a host WITH an admission policy -- an unadmitted connection may
          send admission handshake messages and nothing else. Everything
          that could mutate lobby or game state is inert until it has
          proved possession of the invite.
        * self-delivery -- the local session's own emissions never crossed
          a wire and are not a connection to admit.
        * no admission policy configured -- compat harnesses, which cannot
          be constructed this way on the production transport (see
          __init__: a wire-mode host must be given one).

        Note what this does NOT decide: WHO the peer is. Admission proves
        the peer holds the invite, which everyone invited holds. Seat
        authority is still the Ed25519 binding, checked separately. A
        holder of the room code can still present several identities --
        that is a policy problem this layer does not pretend to solve.
        """
        if conn_id == self.local_conn_id:
            return True

        # --- joiner side: nothing but the handshake until the host is the
        # host. _on_player_ack used to accept the first sender while
        # _host_conn_id was unknown, because that message was historically
        # how a joiner learned who the host was. Under the new handshake
        # that assumption is a hole: wire.unpack proves only that a message
        # was signed by SOME key, so a hostile endpoint could send
        # player_ack, player_list and game_start before the real challenge
        # completes and be believed. The invite-pinned key decides who the
        # host is; nothing else may.
        if self._joiner_admission is not None and not self._host_authenticated:
            if mtype in _ADMISSION_TYPES:
                return True
            _log.warning(
                "session: refusing %r from %s -- no peer has authenticated "
                "as the host pinned by the invite", mtype, conn_id)
            return False

        if self._admission is None or not self.is_host:
            return True
        if self._admission.is_admitted(conn_id):
            # Admission binds the CONNECTION to the key that completed the
            # transcript, so later traffic cannot switch identity. Without
            # this a client could authenticate as K1 -- proving it holds the
            # invite -- and then send player_info signed by K2, which
            # _on_player_info would bind to the seat. It still holds the
            # capability, so this is not catastrophic, but it would make
            # binding joiner_pubkey into the transcript decorative.
            admitted = self._admission.admitted_key(conn_id)
            if admitted is not None:
                author = msg_pubkey
                # A missing author is NOT "nothing to compare". Once
                # conn_id -> K1 exists, a wire message with no usable
                # envelope author is invalid: the transport hands up
                # unsigned relay-control frames, and treating those as
                # exempt let them reach the hash-chain bookkeeping and seed
                # per-peer state from unsigned bytes. Compat harnesses keep
                # the lenient behaviour they were built on; production does
                # not.
                if not isinstance(author, str) or not author:
                    if self.author_mode == AUTHOR_MODE_WIRE:
                        _log.warning(
                            "session: refusing %r on %s -- no envelope "
                            "author on a connection admitted as %s",
                            mtype, conn_id, admitted.hex()[:16])
                        return False
                elif author != admitted.hex():
                    _log.warning(
                        "session: refusing %r on %s -- signed by %s but that "
                        "connection was admitted as %s",
                        mtype, conn_id, author[:16], admitted.hex()[:16])
                    return False
            return True
        if mtype in _ADMISSION_TYPES:
            return True
        _log.warning(
            "session: refusing %r from unadmitted connection %s -- the "
            "admission handshake has not completed", mtype, conn_id)
        return False

    def _normalize_hostless(self, conn_id: str, msg: dict) -> HostlessInbound:
        """The representation boundary: wire envelope or flat dict -> record.

        Exactly one place converts between the two shapes a hostless message
        can arrive in, and it names every identity it extracts instead of
        leaving them to be re-derived downstream.

        ``verified`` records WHICH shape it was, rather than letting later
        code infer it from the presence of a key. That inference is what made
        the compat fallback invisible: a flat dict and a stripped envelope
        look identical once unwrapped.
        """
        body, author, enveloped = self._hostless_projection(msg)
        seat = body.get("seat", body.get("seat_from"))
        if not _is_seat(seat):
            seat = None
        raw_hand = body.get("hand", self._hand_no)
        try:
            hand = None if isinstance(raw_hand, bool) else int(raw_hand)
        except (TypeError, ValueError):
            hand = None

        return HostlessInbound(
            mtype       = msg.get("type"),
            body        = body,
            conn_id     = conn_id,
            author      = author,
            seat        = seat,
            hand        = hand,
            fingerprint = self._envelope_fingerprint(msg, body),
            verified    = enveloped and author is not None,
            local       = conn_id == self.local_conn_id,
            envelope    = msg,
        )

    def _admit_hostless(self, ctx: HostlessInbound) -> bool:
        """Authorize, sequence and relay -- once each. False means drop.

        The single gate between the wire and the typed handlers. Everything
        past this point may assume the message is from the seat it claims.
        """
        if not ctx.local and ctx.seat is not None:
            if not self._author_owns_seat(ctx.conn_id, ctx.author, ctx.seat):
                _log.warning(
                    "session: %s via %s claims seat %s but is not signed by "
                    "that seat's bound key -- dropping",
                    ctx.mtype, ctx.conn_id, ctx.seat)
                return False
            if not self._sequence_ok(ctx):
                return False
        # Relayed only after authorization and the replay/equivocation gate,
        # so the host neither amplifies a message its recipients would reject
        # nor re-broadcasts a replay.
        self._relay_if_host(ctx)
        return True

    def _sequence_ok(self, ctx: HostlessInbound) -> bool:
        """Replay / equivocation gate for an ALREADY-AUTHORIZED message.

        Authorship is settled by the caller. This must not re-decide it: a
        number is only meaningful once it is known whose stream it belongs
        to, and consuming one on an unauthorized message would let a stranger
        desynchronise a real seat.

        Checked for all eight types together, because the counter that
        produces it is stamped for all eight in one place (_send_hostless).
        Validating a subset is worse than validating none -- the sender
        advances on every hostless send, so a receiver watching only some
        types reads the others as gaps.
        """
        if ctx.hand is None:
            return True              # _hand_msg_ok owns malformed hand numbers
        if ctx.hand != self._hand_no:
            # Not a stream we are tracking yet. A future-hand message is
            # buffered by _hand_msg_ok and fed back through handle_message by
            # _replay_buffer, so recording it here would mark it seen on the
            # first pass and drop it as a duplicate on the second -- losing
            # precisely the early key_announce the buffer exists to keep.
            return True
        return self._author_seq_ok(
            ctx.seat, ctx.hand, ctx.body, ctx.fingerprint)

    def _relay_if_host(self, ctx: HostlessInbound) -> None:
        """Host only: forward an ALREADY-AUTHORIZED envelope onward.

        The production topology is a star -- only the host listens, joiners
        only dial it -- while the hostless protocol is peer-symmetric. A
        joiner's message therefore reaches the host and nobody else, and
        a three-seat hand cannot leave KEYGEN. See
        docs/TOPOLOGY_DECISION.md and tests/test_three_peer_topology.py.

        The host is a COURIER, never the author. It forwards the envelope
        it received: same v, type, payload, pubkey, ts, prev, sig and hash.
        It does not re-sign, does not substitute its own pubkey, does not
        rebuild the payload, and does not touch the claimed seat. The frame
        is re-serialized on the way out, which is fine -- wire.unpack
        verifies over canonical field values, not over the original bytes,
        and _sign_frame leaves an already-signed message alone.

        Authorization is NOT repeated here. _admit_hostless has already
        established that ctx.author owns ctx.seat and that the message is
        neither a replay nor an equivocation, and it drops anything that
        fails before calling this. A second lookup here would be a second
        copy of the rule that could drift from the first -- and it did not
        even agree with the first for free: reached through a different
        argument it could reach a different answer, which is the whole
        reason authorization moved to one place.

        Never echoed back to the connection it arrived on: the author has
        already applied it locally, and a returned copy would be a
        duplicate the sequence check would then have to reject.
        """
        if not self.is_host:
            return
        if ctx.local:
            return                       # our own emission, already fanned out
        if ctx.seat is None:
            return                       # unattributable; nothing to forward for
        forward = getattr(self._transport, "broadcast_except", None)
        if forward is None:
            _log.warning("session: transport cannot relay (no "
                         "broadcast_except); %s stays unforwarded", ctx.mtype)
            return
        forward(ctx.conn_id, ctx.envelope)

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

        Retained with this signature because the adversarial suites call it
        directly. The ingress pipeline uses _author_owns_seat, which takes
        the author explicitly instead of digging it back out of a dict.
        """
        return self._author_owns_seat(conn_id, msg.get("pubkey"), seat)

    def _author_owns_seat(self, conn_id: str, author, seat: int) -> bool:
        """Does ``author`` (an Ed25519 pubkey hex) speak for ``seat``?

        The one implementation of the authorization rule. Takes the author
        as an argument rather than re-deriving it, so no caller can lose it
        on the way in -- which is exactly how the #30 defect worked.

        With no bindings established at all the behaviour depends on the
        session's author_mode, and that is now an explicit decision rather
        than an accident of initialisation order:

        * AUTHOR_MODE_WIRE (production) fails CLOSED. Bindings are frozen
          before the first hand from verified envelopes; if they are absent
          when a remote message arrives, the thing that would authorize it
          does not exist, and standing in the delivering connection means
          trusting the hop instead of the author.
        * AUTHOR_MODE_COMPAT (in-memory and unsigned harnesses) uses the
          conn_id rule, because those transports carry no envelopes and
          there is no author to check.
        """
        if not (0 <= seat < len(self._seat_order)):
            return False
        if conn_id == self.local_conn_id:
            return True
        if self._seat_keys:
            expected = self._seat_keys.get(seat)
            return (bool(expected) and isinstance(author, str)
                    and author == expected)
        if self.author_mode == AUTHOR_MODE_WIRE:
            _log.warning(
                "session: no seat keys bound; refusing seat %s from %s in "
                "wire mode rather than trusting the delivering connection",
                seat, conn_id)
            return False
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
        # Author authorization is NOT repeated here: _admit_hostless settled
        # it at ingress for all eight types. An unattributable message (no
        # integer seat) still has to go, though -- ingress lets it through
        # for its type's own handler to judge, and the deal driver routes by
        # seat, so it cannot route this.
        claimed = msg.get("seat", msg.get("seat_from"))
        if not isinstance(claimed, int) or isinstance(claimed, bool):
            _log.warning("session: deal msg via %s has no usable seat "
                         "(%r) — dropping", conn_id, claimed)
            return
        self._deal_driver.handle(dict(msg))
        self._flush_deal()

    def _on_hand_void(self, conn_id: str, msg: dict) -> None:
        """Fail the current hand closed when any authenticated seat voids it."""
        if not self._hand_msg_ok(conn_id, msg):
            return
        # Authorized at ingress; see _admit_hostless. What ingress could NOT
        # authorize is a message whose seat is not an integer -- it extracts
        # the claimed seat with an isinstance check and lets anything else
        # through for the type's own handler to judge. So this must refuse
        # it rather than coerce: int("1") would revive a seat ingress
        # declined to authorize, and apply it unchecked.
        if not _is_seat(msg.get("seat")):
            return
        reason = str(msg.get("reason", "peer voided the hand"))[:512]
        self._void_hand(reason, announce=False)

    def _on_session_end(self, conn_id: str, msg: dict) -> None:
        """Receive final match state, including on already-busted spectators."""
        # Authorized at ingress; see _admit_hostless. The seat is checked for
        # shape only, and NOT coerced -- ingress declines to authorize a
        # non-integer seat, so int("1") here would apply one it refused.
        if not _is_seat(msg.get("seat")):
            return
        try:
            hand = int(msg["hand"])
            stacks = [int(v) for v in msg["stacks"]]
            raw_winner = msg.get("winner")
            winner = None if raw_winner is None else int(raw_winner)
        except (KeyError, TypeError, ValueError):
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
            self._send_hostless(m)              # to the other peers
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
        # Everything that can refuse this hand runs BEFORE any gameplay
        # state exists. Validating after the replica is live produces the
        # worst outcome available: a table that has posted blinds, accepts
        # bets (send_bet_action gates on _replica alone) and can never
        # settle, because settlement needs a deal that was refused. The
        # policy checks below are the ones this mandate added, so they are
        # the ones that turned a theoretical ordering flaw into a reachable
        # one; hoisting them keeps the transition transactional.
        #
        # session_id is built here for the same reason -- it encodes the
        # policy, so it is fallible, and a failure must land before the
        # replica rather than between the replica and the driver.
        self._assert_deal_preconditions()
        session_id = self._deal_session_id()

        self._hand_record = None
        self.void_reason = None
        self.hand_result = None
        self._own_hole_set = False
        self._hand_stacks = list(stacks)
        self._hand_positions = positions
        self._replica = ReplicaTable(
            session_id=session_id, hand_no=hand_no,
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
        try:
            self.begin_hand(hand_no, button=self._replica.button,
                            seats_in=self._replica.seats_dealt)
        except Exception:
            # begin_hand still has refusal paths that need the replica to
            # evaluate (is the local seat dealt in?). If one fires, roll the
            # replica back rather than leaving a bettable table with no deal.
            self._replica = None
            self._hand_stacks = []
            self._hand_positions = None
            raise
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
            self._send_hostless({
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
        self._send_hostless({
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
        # Authorized at ingress; see _admit_hostless. Seat shape is checked
        # rather than coerced, for the same reason as session_end.
        if not _is_seat(msg.get("seat")):
            return
        try:
            seq = int(msg["seq"])
            seat = int(msg["seat"])
            action = str(msg["action"])
            amount = int(msg.get("amount", 0))
        except (KeyError, ValueError, TypeError):
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
            self._send_hostless({
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

    def _adopt_signing_key(self, conn_id: str, claimed) -> str:
        """The ONE place a peer's signing identity is decided. Write-once.

        Three handlers write this field -- _on_player_info, _on_player_list
        and add_local_player -- and the invariant was previously enforced in
        one of them. That is how three repair attempts in a row failed in
        the same place: the property belongs to the FIELD, and a rule that
        lives in a handler is only true for the messages that happen to go
        through it. A one-line player_info walked around a write-once rule
        implemented in player_list.

        Write-once because this key decides who may author for a seat.
        Everything else in a roster entry is presentation or lobby state and
        may be updated freely; this one, once established, is the identity
        that later freezes into a seat. Rotation is not supported and no
        shipped path rotates.

        Returns the key to store. A refused change is logged and the
        established value kept, so a hostile update is inert rather than
        fatal.
        """
        existing = self.players.get(conn_id)
        current = getattr(existing, "ed25519_pubkey_hex", "") if existing else ""
        claimed = claimed if isinstance(claimed, str) else ""
        if not current:
            return claimed
        if claimed and claimed != current:
            _log.warning(
                "session: refusing to move %s from signing key %s to %s -- "
                "a signing identity is write-once", conn_id,
                current[:16], claimed[:16])
        return current

    def _on_player_info(self, conn_id: str, msg: dict) -> None:
        """Host receives identity from a newly connected peer.

        HOST ONLY. This message travels joiner -> host; a joiner receiving
        one is being told about an identity by a peer that has no authority
        to assert it. Processing it let the authenticated host overwrite an
        established signing key on a joiner -- including the host's own,
        which then froze into its seat -- with a message that never went
        near the roster path where write-once was enforced.
        """
        if not self.is_host:
            _log.warning("session: ignoring player_info from %s -- only a "
                         "host receives identity announcements", conn_id)
            return
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
                ed25519_pubkey_hex = self._adopt_signing_key(
                    conn_id, msg.get("pubkey", "")),
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
        # JOINER ONLY. A host is the roster's author and must never adopt
        # one. The existing guard was `if self._host_conn_id and conn_id !=
        # self._host_conn_id`, and a host's _host_conn_id is always "" --
        # written only by mark_host_authenticated (joiner-side) and
        # _elect_new_host (dead in wire mode) -- so it failed open exactly
        # on the host. Any single admitted invitee could inject roster
        # entries, which start_game turns into seats and _bind_seat_keys
        # freezes onto attacker-supplied keys.
        if self.is_host:
            _log.warning("session: ignoring player_list from %s -- the host "
                         "is the roster's author, not a recipient", conn_id)
            return

        payload = msg.get("payload", {})
        players_data = payload.get("players", [])

        # Close the chain. The roster is host-authoritative, so without this
        # a joiner's seat keys are whatever the host asserted -- including
        # for the HOST'S OWN seat, which is the one identity the invite
        # already pinned. The claim "invite key == frozen seat key" was
        # therefore checked nowhere, and a host could seat itself under a
        # different key than the one it authenticated with.
        #
        # Compared over all 32 bytes. An 8-byte comparison here would
        # reintroduce exactly the 64-bit target V2 exists to remove.
        refused: set = set()
        if self._pinned_host_pubkey is not None:
            for p in players_data:
                cid = p.get("conn_id", "")
                if not cid:
                    continue
                # Evaluated on the EFFECTIVE post-merge values, not on what
                # the payload claims.
                #
                # An earlier version tested p.get("is_host") and skipped
                # anything falsy -- but is_host is a field the SENDER
                # chooses, and the merge below preserves the existing flag
                # while unconditionally rewriting the key. So omitting
                # is_host from an update walked straight past the check and
                # still repointed the host's key, which then froze into the
                # host's seat: exactly the substitution this exists to stop,
                # reachable by leaving a field out.
                #
                # Anchor on what WE already believe about the entry, merged
                # with what the payload asks to change, so there is no form
                # of the message that is checked differently from how it is
                # applied.
                existing = self.players.get(cid)
                is_host = p.get("is_host",
                                existing.is_host if existing else False)
                if not is_host:
                    continue
                claimed = p.get(
                    "ed25519_pubkey_hex",
                    existing.ed25519_pubkey_hex if existing else "")
                if claimed and claimed != self._pinned_host_pubkey:
                    # Drop the OFFENDING ENTRY, not the whole message.
                    #
                    # This used to `return`, discarding every other player in
                    # the same roster. A peer that got a bad host entry into
                    # the host's broadcast could therefore freeze every
                    # joiner's lobby permanently -- a denial of service
                    # created by the check itself. The injection route is
                    # closed above, but a check whose failure mode is
                    # "discard everything" should not be relied on for that.
                    _log.warning(
                        "session: dropping roster entry %s -- it would seat "
                        "host key %s but the invite pinned %s",
                        cid, str(claimed)[:16], self._pinned_host_pubkey[:16])
                    refused.add(cid)

        with self._lock:
            for p in players_data:
                cid = p.get("conn_id", "")
                if not cid or cid in refused:
                    continue
                if cid not in self.players:
                    self.players[cid] = Player(
                        conn_id           = cid,
                        peer_id           = p.get("peer_id",           ""),
                        nickname          = p.get("nickname",          "Player"),
                        avatar_b64        = p.get("avatar_b64",        ""),
                        x25519_pubkey_hex = p.get("x25519_pubkey_hex", ""),
                        ed25519_pubkey_hex = self._adopt_signing_key(
                            cid, p.get("ed25519_pubkey_hex", "")),
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
                    # A SIGNING key is write-once. Every other field here is
                    # presentation or lobby state and may be updated; this
                    # one decides who may author for a seat, and a roster
                    # broadcast must not be able to move it.
                    #
                    # Gating this on the is_host flag was tried and is not
                    # enough: the flag is chosen by the sender, and
                    # _bind_seat_keys binds by seat POSITION rather than by
                    # flag, so an update that dropped is_host still poisoned
                    # the key that froze into that seat. The property has
                    # nothing to do with host-ness -- no roster may rewrite
                    # any established signing identity.
                    existing.ed25519_pubkey_hex = self._adopt_signing_key(
                        cid, p.get("ed25519_pubkey_hex",
                                   existing.ed25519_pubkey_hex))
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
        # NOTE: this deliberately no longer sets _host_conn_id. Learning our
        # own id from a message is fine; deciding WHO THE HOST IS from the
        # first peer to speak is not. wire.unpack proves only that a message
        # was signed by some key, so accepting the sender here handed host
        # authority -- which gates pause, resume, kick, adjust_blinds,
        # game_start and session_end -- to whoever transmitted first. The
        # host hop is now established only where the invite-pinned key is
        # verified: see mark_host_authenticated().

    @owned
    def _on_game_start(self, conn_id: str, msg: dict) -> None:
        """Adopt the host's table settings -- once, from the host only.

        This message defines seat order and the table-wide deal policy, and
        both were previously rewritten by any peer at any time. A forged
        game_start mid-hand could repoint every seat index (which reassigns
        hole cards and blame) or downgrade the deal for this peer, which
        nothing else would notice because the policy is read from exactly
        this message.

        Capabilities are therefore frozen once play begins. A duplicate of
        the legitimate message is accepted as a no-op rather than refused,
        so retries and relay echoes stay harmless.

        The policy is parsed and refused BEFORE state becomes PLAYING. That
        ordering is the point: refusing at deal time, as the old
        require_prevention check did, meant a peer had already accepted the
        table -- announced itself as playing, taken a seat, become
        answerable for a hand -- before discovering it disagreed about how
        cards would be dealt.
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
            # Compare the PARSED policy, and tolerate a malformed one. This
            # branch is the reject path: a hostile or stale game_start must
            # be logged and dropped, never raised out of a message handler
            # onto the transport's dispatch thread.
            proposed = self.parse_deal_policy(settings, self.author_mode)
            same = (list(payload.get("seat_order", [])) == list(self._seat_order)
                    and proposed == self._deal_policy)
            if not same:
                _log.warning(
                    "session: game_start from %s would change settled table "
                    "settings mid-session — ignoring", conn_id)
            return
        settings = payload.get("table_settings", {})
        policy = self.parse_deal_policy(settings, self.author_mode)
        if policy is None:
            declared = (settings.get(self.DEAL_POLICY_SETTING)
                        if isinstance(settings, dict) else None)
            self.terminate(
                self.POLICY_REFUSED,
                f"table declared deal policy {declared!r}, which this peer "
                f"will not play under in {self.author_mode}",
                conn_id=conn_id)
            return
        if not self._adopt_deal_policy(policy):
            self.terminate(
                self.POLICY_REFUSED,
                f"table changed deal policy from {self._deal_policy!r} to "
                f"{policy!r}",
                conn_id=conn_id)
            return
        self.state = "PLAYING"
        self._seat_order = payload.get("seat_order", [])
        # Store table settings so _mp_new_game in gui.py can read them
        ts = payload.get("table_settings", {})
        if ts:
            self._last_table_settings = ts
        # The policy was adopted above, before PLAYING. Nothing re-reads it
        # here: this used to be the second of three places that derived the
        # mode from a settings dict, and three readers of one field is how
        # they drift.
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
            session_id=self._recorded_session_id(),
            hand_no=self._hand_no,
            previous_state=previous,
            terminal_state=state,
            terminal_reason=reason,
            initiating_seat=seat,
            conn_id=conn_id,
            host_conn_id=self._host_conn_id,
            monotonic_ts=time.monotonic(),
            sequence=self._terminal_seq,
            deal_policy=self._deal_policy,
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
        # Admission is connection-scoped, so it dies with the connection --
        # cleared even on a terminal session, because conn_ids can be reused
        # and stale admission is the one piece of state that must never
        # outlive its socket. A reconnecting peer redoes the whole exchange
        # against a fresh nonce, which is what makes a captured response
        # from an earlier connection worthless.
        if self._admission is not None:
            self._admission.forget(conn_id)

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
        # Wire mode does not migrate hosts, and this is a protocol
        # statement rather than a missing feature.
        #
        # V2 authentication pins ONE exact 32-byte host key, carried in the
        # invite every joiner holds. Promoting a different peer changes
        # that identity, so the invite can no longer authenticate the host
        # it names -- and every joiner's pin is now wrong. Migration would
        # need authenticated authority transfer, reconnection, and in
        # practice a new invite: a separate protocol, not a repair to this
        # one.
        #
        # It is also how the M-8 gate was reopened at runtime. Election set
        # is_host = True on a Session constructed with admission=None, and
        # _admission_ok short-circuits on a missing policy, so a promoted
        # peer admitted anyone. The constructor invariant only ever covered
        # construction.
        #
        # Compat harnesses keep the old behaviour; that is what the mode is
        # for. Production fails closed and a new host needs a new lobby.
        if self.author_mode == AUTHOR_MODE_WIRE:
            _log.warning(
                "session: host lost in LOBBY; refusing to elect a "
                "replacement because the invite pins one exact host key. "
                "A new host requires a newly authenticated lobby.")
            self.terminate(self.HOST_LOST,
                           "host lost in lobby; wire mode does not migrate "
                           "a pinned host identity")
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
                # Through the chokepoint like every other writer, even
                # though this one supplies OUR OWN key rather than anything
                # a peer sent. An unconditional write here re-opens
                # write-once for that conn_id, and "this caller is
                # trustworthy so it may skip the rule" is precisely the
                # exemption that produced the last two findings. Idempotent
                # in practice: the first call establishes the key and later
                # calls present the same value.
                ed25519_pubkey_hex = self._adopt_signing_key(
                    conn_id, _id.public_key_bytes().hex()),
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
        # Parsed AFTER the terminal and host checks, so those keep winning:
        # a terminated session must still report that it is terminated
        # rather than complaining about a settings dict it will never use.
        policy = self.parse_deal_policy(table_settings, self.author_mode)
        if policy is None:
            declared = (table_settings.get(self.DEAL_POLICY_SETTING)
                        if isinstance(table_settings, dict) else None)
            raise ValueError(
                f"cannot start a game: table_settings[{self.DEAL_POLICY_SETTING!r}] "
                f"is {declared!r}; {self.author_mode} requires "
                f"{self.DEAL_POLICY_BG!r}"
                + ("" if self.author_mode == AUTHOR_MODE_WIRE else
                   f" or {self.DEAL_POLICY_DETECTION!r}")
                + ". A table must declare how it deals; there is no default.")
        if not self._adopt_deal_policy(policy):
            raise RuntimeError(
                f"cannot start a game: deal policy is already "
                f"{self._deal_policy!r} and cannot change to {policy!r}")
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
    def seat_local_table(self, order: list[str],
                         nicknames: Optional[dict] = None) -> None:
        """Seat a table whose every seat lives in this process.

        The sidecar's non-human seats are AI players sharing one process and
        one device identity -- not remote peers. They have no distinct
        signing key and never will, because holdem.p2p.identity holds a
        process-global private key. So the lobby handshake does not describe
        them: routing them through player_info would make each seat dial the
        host it already is, and _on_player_info hardcodes is_host=False, so
        the resulting roster would claim the table has no host.

        Rather than have local seats impersonate joiners, a local table says
        what it is. Seats are keyless BY INTENT, not by omission: with no
        ed25519 key on any Player, _bind_seat_keys leaves _seat_keys empty
        and _author_owns_seat stays on the compat conn_id rule, which is the
        correct authority when the bus itself is authoritative about who
        sent what. A partially-keyed roster is the failure mode this avoids
        -- one keyed seat makes _seat_keys truthy and every OTHER seat's
        messages get refused, which presents as a table that deals nothing.

        Refused outright in AUTHOR_MODE_WIRE. On a transport carrying real
        envelopes, seats are earned through the admission handshake; this
        seam must never become a way to seat a peer without one.
        """
        if self.author_mode == AUTHOR_MODE_WIRE:
            raise RuntimeError(
                "seat_local_table is for local tables only; a session on a "
                "verified-envelope transport seats peers through admission")
        if self.terminal_state is not None:
            raise RuntimeError(
                f"cannot seat a table: session terminated "
                f"({self.terminal_state}: {self.terminal_reason})")
        # configure_seats owns the structural rules (2-9 seats, unique ids,
        # local seat present, no seating mid-hand). Run it FIRST so an
        # invalid order is refused before any roster state is written.
        self.configure_seats(order)
        nicknames = nicknames or {}
        with self._lock:
            self.players.clear()
            self._join_order.clear()
            for cid in order:
                self.players[cid] = Player(
                    conn_id    = cid,
                    peer_id    = "",
                    nickname   = nicknames.get(cid, cid),
                    avatar_b64 = "",
                    is_host    = (cid == order[0]),
                )
                if cid != self.local_conn_id:
                    self._join_order.append(cid)
        if self.on_player_list_changed:
            self.on_player_list_changed(list(self.players.values()))

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
        # Sequence holes are read HERE and nowhere earlier. A hole is not a
        # failure on its own -- delivery may reorder, and a late message
        # closes it with no gameplay consequence -- so it is only evidence
        # once an existing deadline has already decided that waiting has
        # become failure. This adds no timer and shortens no deadline: the
        # timeout fires on exactly the schedule it always did, and the holes
        # ride along to say what was observed missing when it did.
        holes = self.author_seq_hole_report()
        self._safe_emit(
            "timeout_proposed",
            hand        = self._hand_no,
            seq         = token.action_seq,
            phase       = self._replica.phase if self._replica else "unknown",
            actor       = token.actor,
            token_phase = token.phase,
            author_seq_holes = holes,
        )
        self._send_hostless({
            "type":         "timeout_proposal",
            "hand":         self._hand_no,
            "token": {
                "hand_id":    token.hand_id,
                "phase":      token.phase,
                "actor":      token.actor,
                "action_seq": token.action_seq,
            },
            "missing_seat": None,
            "author_seq_holes": holes,
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
        # Forensics, not cause. The timeout remains the liveness reason; the
        # holes only say what was already missing when it fired. When the
        # suppressed message is the LAST one in a stream there is no hole to
        # report and this is empty -- the timeout behaves exactly as before.
        self._safe_emit(
            "timeout_applied",
            hand        = self._hand_no,
            seq         = token.action_seq,
            phase       = self._replica.phase if self._replica else token.phase,
            digest      = self._replica.state_digest() if self._replica else "",
            actor       = token.actor,
            token_phase = token.phase,
            author_seq_holes = self.author_seq_hole_report(),
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
