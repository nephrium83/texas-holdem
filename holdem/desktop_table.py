"""Desktop composition: choose a local bot table or an authenticated LAN table."""
from __future__ import annotations

import random
import threading

from holdem import client_view, player_info
from holdem.p2p.session import Session


class DesktopTable:
    """Own the current table; protocol state stays inside its Session.

    The localhost server runs these synchronous operations off its event loop.
    Remote callbacks only mark the client dirty, never acquire this lock.
    """

    def __init__(self, *, seats, nickname, settings, seed=None, on_change):
        self._lock = threading.RLock()
        self._seats = seats
        self._nickname = nickname
        self._settings = dict(settings)
        self._seed = seed
        self._on_change = on_change
        self._mode = "menu"
        self._local = None
        self._lan = None
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
            if command in ("local_game", "host_lan", "join_lan"):
                if self._mode != "menu":
                    raise ValueError("Leave the current table before choosing another game")
                self._nickname = self._name(payload)
                if command == "local_game":
                    self._create_local()
                else:
                    from holdem.lan_session import LanTable
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
                if self._lan is None or type(payload.get("ready")) is not bool:
                    raise ValueError("Ready requires a LAN table and a true/false value")
                self._lan.set_ready(payload["ready"])
                return self._result(command, "applied")
            if self._lan is not None:
                if command == "start_game":
                    verdict = self._lan.start()
                    return self._result(command, verdict, verdict == "started")
                session = self._lan.session
                if session is None:
                    raise ValueError("The table is closed")
                return client_view.apply_command(session, command, payload)
            human = self._local[1][self._local[2][0]]
            return client_view.apply_command(human, command, payload,
                                             start_table=self._start_local)

    @staticmethod
    def _result(command, verdict, ok=True):
        return {"type": "command_result", "command": command,
                "ok": ok, "verdict": verdict}

    def _create_local(self):
        from holdem.sidecar_launcher import (
            BotDriver, _make_sessions, _wrap_with_drain,
            _wire_hand_start, _make_start_table,
        )
        bus, sessions, order = _make_sessions(self._seats, nickname=self._nickname)
        human = sessions[order[0]]
        _wrap_with_drain(human, bus)
        BotDriver({cid: session for cid, session in sessions.items()
                   if cid != order[0]}, random.Random(self._seed))
        _wire_hand_start(sessions, order)
        self._start_local = _make_start_table(sessions, order, bus, dict(self._settings))
        human.on_state_changed = self._on_change
        self._local = (bus, sessions, order)
        self._mode = "local"

    def snapshot(self):
        with self._lock:
            if self._lan is not None:
                session = self._lan.session
                if session is not None:
                    with session.read_state():
                        snap = client_view.snapshot(session)
                        lobby = self._lan.lobby_state()
                else:
                    snap = self._empty_snapshot()
                    lobby = self._lan.lobby_state()
            elif self._local is not None:
                human = self._local[1][self._local[2][0]]
                with human.read_state():
                    snap = client_view.snapshot(human)
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
        if self._lan is not None:
            self._lan.close()
            self._lan = None
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
