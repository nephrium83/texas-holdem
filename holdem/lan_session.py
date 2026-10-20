"""One local player's authenticated LAN table, independent of the renderer.

The transport is process scoped, so a sidecar owns one table at a time.
Leaving tears it down before the same sidecar can host or join another room.
Only public Session methods are used here; Session remains the protocol owner.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from holdem import native_crypto
from holdem.p2p import admission, device_secret, identity, invite, transport
from holdem.p2p.join_auth import JoinAuthenticator, joiner_admission_from_invite
from holdem.p2p.session import Session
from holdem.p2p.casual_clock import PROFILE

_log = logging.getLogger(__name__)

# Bounds only initial socket admission and roster delivery. Poker hand and
# betting deadlines remain entirely Session-owned and are unchanged here.
JOIN_STARTUP_TIMEOUT = 15.0


def _settings(settings: dict) -> dict:
    values = {"sb": 25, "bb": 50, "stack": 1000,
              "structure": "No-Limit", "deal_policy": Session.DEAL_POLICY_BG,
              "casual_profile": PROFILE}
    values.update(settings)
    for field in ("sb", "bb", "stack"):
        if type(values[field]) is not int:
            raise ValueError(f"{field} must be a whole number")
    if values["sb"] <= 0 or values["bb"] <= values["sb"]:
        raise ValueError("Blinds must be positive and the big blind must be larger")
    if values["stack"] < values["bb"]:
        raise ValueError("Starting chips must cover the big blind")
    if values["structure"] not in ("No-Limit", "Pot-Limit", "Fixed-Limit"):
        raise ValueError("Unsupported betting structure")
    if values["deal_policy"] != Session.DEAL_POLICY_BG:
        raise ValueError("LAN games require Bayer-Groth dealing")
    if values["casual_profile"] != PROFILE:
        raise ValueError("All players need the same Poker Preview version")
    return values


class LanTable:
    """Host or join one LAN room using signed transport and V2 admission.

    ``on_change`` is a dirty notification, callable from any thread. It must
    not synchronously render a snapshot or wait for the UI's event loop.
    The caller reads ``lobby_state`` on its own thread when ready to render.
    """

    def __init__(self, nickname: str, settings: dict,
                 on_change: Callable[[], None] | None = None):
        if not isinstance(nickname, str) or not nickname.strip():
            raise ValueError("Enter a player name")
        if len(nickname) > 40:
            raise ValueError("Player names must be at most 40 characters")
        self.nickname = nickname
        self.settings = _settings(settings)
        self._on_change = on_change
        self._lock = threading.RLock()
        self._operations = threading.RLock()
        self._generation = 0
        self._session: Session | None = None
        self._mode = "host"
        self._state = "closed"
        self._invite = ""
        self._error: str | None = None
        self._host_connection: str | None = None
        self._authenticator: JoinAuthenticator | None = None
        self._startup_watchdog: threading.Timer | None = None
        self._ticker_stop: threading.Event | None = None
        self._ticker_thread: threading.Thread | None = None

    def _start_ticker(self, generation: int, session: Session) -> None:
        stop = threading.Event()

        def run():
            while not stop.wait(0.25):
                if not self._current(generation, session):
                    return
                try:
                    session.reliability_tick()
                except Exception:
                    _log.exception("LAN reliability tick failed")
                    session.terminate(Session.ABORTED_PROTOCOL,
                                      "The table stopped because its clock could not advance")
                    return

        thread = threading.Thread(target=run, name="poker-lan-clock", daemon=True)
        with self._lock:
            if not self._current(generation, session) or self._state == "closed":
                return
            self._ticker_stop, self._ticker_thread = stop, thread
        thread.start()

    @property
    def session(self) -> Session | None:
        with self._lock:
            return self._session

    def _notify(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                _log.exception("LAN state notification failed")

    def _current(self, generation: int, session: Session) -> bool:
        with self._lock:
            return (generation == self._generation
                    and self._session is session)

    def _changed(self, generation: int, session: Session) -> None:
        if self._current(generation, session):
            self._notify()

    def _roster_changed(self, generation: int, session: Session,
                        players: list) -> None:
        # Called under the Session owner. Do not acquire Session from under
        # the metadata lock in the reverse direction elsewhere.
        present = any(p.conn_id == session.local_conn_id for p in players)
        with self._lock:
            if generation != self._generation or self._session is not session:
                return
            if present and self._state == "connecting":
                self._state = "lobby"
                watchdog, self._startup_watchdog = self._startup_watchdog, None
            else:
                watchdog = None
        if watchdog is not None:
            watchdog.cancel()
        self._notify()

    def _terminated(self, generation: int, session: Session, _record) -> None:
        with self._lock:
            if generation != self._generation or self._session is not session:
                return
            self._state = "closed"
            self._invite = ""
            watchdog, self._startup_watchdog = self._startup_watchdog, None
            ticker_stop = self._ticker_stop
            if session.terminal_state != Session.ENDED_NORMAL:
                self._error = str(session.terminal_reason or "Table closed")[:512]
        if ticker_stop is not None:
            ticker_stop.set()
        if watchdog is not None:
            watchdog.cancel()
        self._notify()

    def _arm_startup_watchdog(self, generation: int, session: Session) -> None:
        watchdog = threading.Timer(
            JOIN_STARTUP_TIMEOUT, self._startup_expired, args=(generation, session))
        watchdog.daemon = True
        with self._lock:
            if (generation != self._generation or self._session is not session
                    or self._state != "connecting"):
                return
            self._startup_watchdog = watchdog
        watchdog.start()

    def _startup_expired(self, generation: int, session: Session) -> None:
        # This is the watchdog's own thread, not the transport consumer.
        # It may stop and join transport after dropping the metadata lock.
        with self._operations:
            with self._lock:
                expired = (generation == self._generation
                           and self._session is session
                           and self._state == "connecting")
                if expired:
                    # Claim the timeout atomically against a roster callback
                    # just arriving. Once claimed, that callback cannot
                    # resurrect the expired attempt while cleanup starts.
                    self._generation += 1
                    self._state = "closed"
            if not expired:
                return
            self._failed("The room did not finish connecting. It may have already started. "
                         "Ask the host for a new invite.")

    def _game_started(self, generation: int, session: Session,
                      payload: dict) -> None:
        if not self._current(generation, session):
            return
        try:
            if payload.get("table_settings", {}).get("casual_profile") != PROFILE:
                raise ValueError("All players need the same Poker Preview version")
            if not session.reliability_lobby_status()["ready"]:
                raise ValueError("The roster clocks have not agreed")
            settings = _settings(payload.get("table_settings", {}))
            order = session.seat_order
            if not 2 <= len(order) <= 9:
                raise ValueError("A LAN table needs two to nine players")
            names = {p.conn_id: p.nickname for p in session.player_list()}
            with self._lock:
                if generation != self._generation or self._session is not session:
                    return
                self._state = "playing"
                self._invite = ""
                watchdog, self._startup_watchdog = self._startup_watchdog, None
            if watchdog is not None:
                watchdog.cancel()
            session.reliability_started()
            session.start_p2p_hand(
                hand_no=1, names=[names.get(cid, "Player") for cid in order],
                stacks=[settings["stack"]] * len(order),
                sb=settings["sb"], bb=settings["bb"],
                structure=settings["structure"], button=0,
            )
        except (ValueError, RuntimeError, TypeError):
            # Do not put a peer-controlled settings blob in logs or errors.
            session.terminate(Session.ABORTED_PROTOCOL,
                              "The table could not start its first hand")
            _log.exception("LAN first hand failed")
        self._changed(generation, session)

    def _install(self, *, is_host: bool, host_admission=None,
                 joiner_admission=None) -> tuple[int, Session]:
        session = Session(
            is_host=is_host, nickname=self.nickname, avatar_b64="",
            transport=transport, clock=transport.get_clock(),
            secret_provider=device_secret.load_or_create,
            admission=host_admission, joiner_admission=joiner_admission,
        )
        session.enable_casual_reliability()
        with self._lock:
            generation = self._generation
            self._session = session
        session.on_player_list_changed = lambda players: self._roster_changed(
            generation, session, players)
        session.on_game_start = lambda payload: self._game_started(
            generation, session, payload)
        session.on_session_terminated = lambda record: self._terminated(
            generation, session, record)
        session.on_state_changed = lambda: self._changed(generation, session)
        transport.reset_callbacks()

        def disconnected(conn_id, reason="disconnect"):
            if not self._current(generation, session):
                return
            with self._lock:
                host_connection = self._host_connection
                connecting = self._state == "connecting"
            if not is_host and conn_id == host_connection:
                # A lobby successor has no host capability for this invite.
                # End visibly rather than letting the legacy Tk host election
                # turn this joiner into an unarmed replacement host.
                display_reason = ("The room closed or already started. Ask the host for a new invite."
                          if connecting else
                          "The host disconnected. Leave and join a new room.")
                if reason == "clock":
                    # The transport reports this only after validating the
                    # timestamped envelope's signature and hash.
                    display_reason = ("Clocks differ too much. Check automatic date and time "
                              "on this computer and the host, then join again.")
                session.terminate(Session.HOST_LOST, display_reason)
            session.handle_disconnect(conn_id, reason=reason)
            self._changed(generation, session)

        transport.on_disconnect(disconnected, with_reason=True)
        return generation, session

    def _begin(self, mode: str) -> None:
        self._close(notify=False)
        with self._lock:
            self._mode = mode
            self._state = "connecting"
            self._error = None
        self._notify()
        native_crypto.initialize()

    def _failed(self, message: str) -> None:
        self._close(notify=False)
        with self._lock:
            self._error = message
        self._notify()

    def host(self) -> None:
        """Open a LAN listener and expose a fresh address-bearing V2 invite."""
        with self._operations:
            try:
                self._begin("host")
                local = identity.load_or_create()
                first_code = invite.generate_room_code(local.public_key_bytes())
                parsed = invite.parse_room_code(first_code)
                policy = admission.HostAdmission(
                    admission_secret=bytes.fromhex(parsed["admission_secret"]),
                    host_pubkey=local.public_key_bytes(),
                    discovery_token=bytes.fromhex(parsed["discovery_token"]),
                )
                generation, session = self._install(is_host=True,
                                                    host_admission=policy)
                def route_host(cid, message):
                    if not self._current(generation, session):
                        return
                    with session.read_state():
                        if (session.state != "LOBBY"
                                and not any(p.conn_id == cid
                                            for p in session.player_list())):
                            # An old invite is not a seat in a running game.
                            # Disconnect before starting a handshake whose
                            # later player_info Session would have to ignore.
                            transport.disconnect(cid)
                            return
                        session.handle_message(cid, message)

                transport.on_message(route_host)
                address = transport.start_host(0)
                ip, port = address.rsplit(":", 1)
                code = invite.generate_room_code(
                    local.public_key_bytes(), public_address=(ip, int(port)),
                    discovery_token=parsed["discovery_token"],
                    admission_secret=parsed["admission_secret"],
                )
                session.local_conn_id = local.peer_id()
                session.add_local_player(
                    session.local_conn_id,
                    public_key=local.public_key_bytes(),
                    x25519_public_key=local.x25519_public_key_bytes(),
                )
                with self._lock:
                    self._invite = code
                self._start_ticker(generation, session)
                self._notify()
            except Exception:
                self._failed("Could not host a LAN game. Check your network and try again.")
                raise

    def join(self, code: str) -> None:
        """Join using just an address-bearing V2 invite; never weaken its pin."""
        # Reject malformed/old codes before closing the current table and,
        # in particular, before any socket can be opened.
        parsed = invite.parse_room_code(code)
        if not parsed["public_ip"] or not parsed["public_port"]:
            raise ValueError("This invite has no LAN address. Ask the host for a new invite.")
        with self._operations:
            try:
                self._begin("join")
                local = identity.load_or_create()
                pin = joiner_admission_from_invite(
                    parsed, joiner_pubkey=local.public_key_bytes())
                generation, session = self._install(is_host=False,
                                                    joiner_admission=pin)

                def failed(_reason):
                    if self._current(generation, session):
                        session.terminate(Session.ABORTED_PROTOCOL,
                                          "The room could not authenticate this connection")

                authenticator = JoinAuthenticator(
                    transport=transport, session=session, joiner_admission=pin,
                    nickname=self.nickname, signer=local, clock=transport.get_clock(),
                    on_failed=failed,
                )
                self._authenticator = authenticator
                transport.on_message(lambda cid, message: (
                    authenticator.route(cid, message)
                    if self._current(generation, session) else None))
                cid = transport.connect(
                    f"{parsed['public_ip']}:{parsed['public_port']}")
                with self._lock:
                    self._host_connection = cid
                self._arm_startup_watchdog(generation, session)
                authenticator.begin(cid)
                self._start_ticker(generation, session)
                self._changed(generation, session)
            except Exception:
                self._failed("Could not join the LAN game. Check the invite and local network.")
                raise

    def set_ready(self, ready: bool) -> None:
        if type(ready) is not bool:
            raise ValueError("Ready must be true or false")
        with self._operations:
            session = self.session
            if session is None:
                raise RuntimeError("Create or join a room first")
            with session.read_state():
                if session.state != "LOBBY" or session.terminal_state is not None:
                    raise RuntimeError("Ready can only change in the lobby")
                if not any(p.conn_id == session.local_conn_id
                           for p in session.player_list()):
                    raise RuntimeError("Wait for the room to finish connecting")
                if session.is_host:
                    session.set_ready(session.local_conn_id, ready)
                else:
                    with self._lock:
                        cid = self._host_connection
                    if not cid:
                        raise RuntimeError("Wait for the room to finish connecting")
                    transport.send(cid, {"type": "ready", "payload": {"ready": ready}})
            self._notify()

    def start(self) -> str:
        with self._operations:
            session = self.session
            if session is None:
                raise RuntimeError("Create a room first")
            if not session.is_host:
                raise ValueError("Only the host can start the game")
            with session.read_state():
                if session.state != "LOBBY" or session.terminal_state is not None:
                    return "already_started"
                if not session.reliability_lobby_status()["ready"]:
                    return "not_ready"
                return session.start_ready_game(dict(self.settings))

    def _close(self, *, notify: bool) -> None:
        # Invalidate callbacks first, then release metadata before waiting
        # for either Session's owner or transport's dispatch thread.
        with self._lock:
            self._generation += 1
            session, self._session = self._session, None
            self._state = "closed"
            self._invite = ""
            self._error = None
            self._host_connection = None
            self._authenticator = None
            watchdog, self._startup_watchdog = self._startup_watchdog, None
            ticker_stop, self._ticker_stop = self._ticker_stop, None
            ticker_thread, self._ticker_thread = self._ticker_thread, None
        if ticker_stop is not None:
            ticker_stop.set()
        if watchdog is not None:
            # Do not join here: an already-fired timer may be waiting for
            # _operations, which the caller of close/rehost currently owns.
            # Its generation check makes that late callback inert.
            watchdog.cancel()
        try:
            if session is not None:
                session.terminate(Session.LOCAL_SHUTDOWN, "You left the room")
            if ticker_thread is not None and ticker_thread is not threading.current_thread():
                ticker_thread.join(timeout=5.0)
        finally:
            try:
                transport.stop()
            finally:
                transport.reset_callbacks()
        if notify:
            self._notify()

    def close(self) -> None:
        """Leave and release all owned sockets before returning. Idempotent."""
        with self._operations:
            self._close(notify=True)

    def lobby_state(self) -> dict:
        # Never hold metadata while acquiring Session's owner. Session
        # callbacks acquire these in the opposite direction.
        with self._lock:
            session = self._session
            generation = self._generation
            result = {"mode": self._mode, "state": self._state,
                      "invite": self._invite if self._mode == "host" else "",
                      "nickname": self.nickname, "players": [],
                      "can_start": False, "error": self._error}
        if session is not None:
            with session.read_state():
                result["players"] = [
                    {"seat": seat, "name": p.nickname, "ready": bool(p.ready),
                     "is_you": p.conn_id == session.local_conn_id,
                     "is_host": bool(p.is_host)}
                    for seat, p in enumerate(session.player_list())
                ]
                result["can_start"] = (
                    session.is_host and session.state == "LOBBY"
                    and session.terminal_state is None
                    and 2 <= len(result["players"]) <= 9 and session.all_ready)
                clocks = session.reliability_lobby_status()
                if session.state == "LOBBY":
                    result["can_start"] = result["can_start"] and clocks["ready"]
                    if not clocks["ready"] and len(result["players"]) > 1:
                        result["error"] = clocks["error"]
            with self._lock:
                changed = generation != self._generation or session is not self._session
            if changed:
                return self.lobby_state()
        return result
