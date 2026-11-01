"""Desktop composition for bots, authenticated LAN and direct Internet play."""
from __future__ import annotations

import random
import threading

from holdem import player_info
from holdem.embedding import SessionPlayer
from holdem.p2p.session import Session


class DesktopTable:
    """Own the current table; protocol state stays inside its Session.

    The localhost server runs these synchronous operations off its event loop.
    Remote callbacks only mark the client dirty, never acquire this lock.
    """

    def __init__(self, *, seats, nickname, settings, seed=None, on_change,
                 local_clock=None, local_schedule=None, loop=None, internet_services=None):
        self._lock = threading.RLock()
        self._seats = seats
        self._nickname = nickname
        self._settings = dict(settings)
        self._seed = seed
        self._on_change = on_change
        self._mode = "menu"
        self._local = None
        self._local_bots = None
        self._local_clock = local_clock
        self._local_schedule = local_schedule
        self._lan = None
        self._internet = None
        self._loop = loop
        self._internet_services = internet_services
        self._start_local = None
        self._closed = False

    @staticmethod
    def _name(payload):
        name = payload.get("nickname", "Player")
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 32
                or not name.isprintable()):
            raise ValueError("Choose a name between 1 and 32 characters")
        return name.strip()

    def apply_command(self, command, payload):
        if not isinstance(payload, dict):
            raise ValueError("Command payload must be an object")
        with self._lock:
            if self._closed:
                raise ValueError("The application is closing")
            if command == "leave_table":
                self._close_active()
                self._on_change()
                return self._result(command, "left")
            if command in ("local_game", "host_lan", "join_lan", "host_internet", "join_internet"):
                if self._mode != "menu":
                    raise ValueError("Leave the current table before choosing another game")
                nickname = self._name(payload)
                if command in ("host_internet", "join_internet"):
                    if self._loop is None:
                        raise ValueError("Internet play is unavailable. Restart Poker and try again.")
                    from holdem.internet_session import InternetTable
                    candidate = InternetTable(loop=self._loop, nickname=nickname,
                                              settings=dict(self._settings), on_change=self._on_change,
                                              services=self._internet_services)
                    try:
                        if command == "host_internet":
                            candidate.host()
                        else:
                            candidate.join(payload.get("invite", ""))
                    except BaseException:
                        candidate.close()
                        raise
                    self._internet = candidate
                    self._nickname = nickname
                    self._mode = "host" if command == "host_internet" else "join"
                elif command == "local_game":
                    self._nickname = nickname
                    self._create_local()
                else:
                    from holdem.lan_session import LanTable
                    self._nickname = nickname
                    self._lan = LanTable(self._nickname, dict(self._settings), self._on_change)
                    self._mode = "host" if command == "host_lan" else "join"
                    if command == "host_lan":
                        self._lan.host()
                    else:
                        self._lan.join(payload.get("invite", ""))
                self._on_change()
                return self._result(command, "created")
            if self._mode == "menu":
                raise ValueError("Choose a game first")
            if command == "set_ready":
                peer_table = self._internet if self._internet is not None else self._lan
                if peer_table is None or type(payload.get("ready")) is not bool:
                    raise ValueError("Ready requires a peer table and a true/false value")
                peer_table.set_ready(payload["ready"])
                return self._result(command, "applied")
            if self._internet is not None:
                if command == "start_game":
                    verdict = self._internet.start()
                    return self._result(command, verdict, verdict == "started")
                return self._internet.command(command, payload)
            if self._lan is not None:
                if command == "start_game":
                    verdict = self._lan.start()
                    return self._result(command, verdict, verdict == "started")
                session = self._lan.session
                if session is None:
                    raise ValueError("The table is closed")
                return SessionPlayer(session).command(command, payload)
            human = self._local[1][self._local[2][0]]
            try:
                return SessionPlayer(human, start_table=self._start_local).command(command, payload)
            finally:
                # Start/deal and human actions drain first. Only then may
                # bots schedule work, leaving the dealt hand visible.
                self._local_bots.refresh()

    @staticmethod
    def _result(command, verdict, ok=True):
        return {"type": "command_result", "command": command,
                "ok": ok, "verdict": verdict}

    def _create_local(self):
        from holdem.sidecar_launcher import (
            _make_sessions, _wrap_with_drain,
            _wire_hand_start, _make_start_table,
        )
        from holdem.paced_bots import PacedBotDriver
        bus, sessions, order = _make_sessions(self._seats, nickname=self._nickname)
        human = sessions[order[0]]
        _wrap_with_drain(human, bus)
        self._local_bots = PacedBotDriver(
            {cid: session for cid, session in sessions.items()
             if cid != order[0]}, bus, random.Random(self._seed),
            lock=self._lock, on_change=self._on_change,
            on_error=self._local_bot_error,
            clock=self._local_clock, schedule=self._local_schedule,
        )
        _wire_hand_start(sessions, order)
        local_settings = dict(self._settings,
                              continuation_policy=Session.CONTINUATION_END_TABLE)
        self._start_local = _make_start_table(sessions, order, bus, local_settings)
        human.on_state_changed = self._on_change
        self._local = (bus, sessions, order)
        self._mode = "local"

    def _local_bot_error(self):
        """Stop an uncertain local hand, retaining its agreed chip balances."""
        if self._local is None:
            return
        reason = ("A local opponent could not finish its turn. "
                  "Leave the table and start a new game.")
        for session in self._local[1].values():
            session.terminate(Session.LOCAL_SHUTDOWN, reason)
        self._on_change()

    def snapshot(self):
        with self._lock:
            if self._internet is not None:
                return self._internet.snapshot()
            if self._lan is not None:
                session = self._lan.session
                if session is not None:
                    with session.read_state():
                        snap = SessionPlayer(session).view()
                        lobby = self._lan.lobby_state()
                else:
                    snap = self._empty_snapshot()
                    lobby = self._lan.lobby_state()
            elif self._local is not None:
                human = self._local[1][self._local[2][0]]
                with human.read_state():
                    snap = SessionPlayer(human).view()
                    lobby = dict(
                        mode="local", state="lobby" if human.state == "LOBBY" else "playing",
                        nickname=self._nickname, invite="", error="",
                        can_start=human.state == "LOBBY" and human.terminal_state is None,
                        players=[dict(seat=i, name=p.nickname, ready=True,
                                      is_you=p.conn_id == human.local_conn_id, is_host=p.is_host)
                                 for i, p in enumerate(human.player_list())],
                    )
            else:
                snap = self._empty_snapshot()
                lobby = dict(mode="menu", state="idle", nickname=self._nickname,
                             invite="", players=[], can_start=False, error="")
            snap["lobby"] = lobby
            return snap

    @staticmethod
    def _empty_snapshot():
        return dict(type="snapshot", phase="lobby", hand_num=0,
                    seats=[], you={"seat": -1}, deal_policy=None,
                    session_over=False, terminal=None, events=[], settlement=None,
                    turn=dict(state="lobby", headline="Choose a game", street="idle",
                              actor=-1, pot=0, your_stack=0, effective_stack=0),
                    deal_progress=player_info.deal_progress_view("lobby"))

    def _close_active(self):
        if self._local_bots is not None:
            self._local_bots.close()
            self._local_bots = None
        if self._lan is not None:
            self._lan.close()
            self._lan = None
        if self._internet is not None:
            self._internet.close()
            self._internet = None
        if self._local is not None:
            for session in self._local[1].values():
                session.on_state_changed = None
                session.terminate(Session.LOCAL_SHUTDOWN, "Left table")
            self._local = None
        self._start_local = None
        self._mode = "menu"

    def close(self):
        with self._lock:
            self._closed = True
            self._close_active()
