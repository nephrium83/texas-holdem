"""Host turn routing and state broadcast in the legacy Tkinter harness
(audit findings 3 and 4).

Finding 3: the host's loop() handed every non-local seat to ai_turn(), so
the host's AI played remote humans' hands and their real actions, arriving
later, were discarded. Finding 4: hero(), ai_turn(), _forced() and street
advances changed state without broadcast_game_state(), so peers lagged.

Methods are called unbound on a SimpleNamespace stand-in (the pattern of
test_identity_injection.py) over a REAL Engine, so no display is needed.
"""
import random
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem import gui
from holdem.engine import Engine, Player
from holdem.gui import Holdem


class _Root:
    def __init__(self):
        self.scheduled = []

    def after(self, _ms, fn=None):
        self.scheduled.append(fn)


class _Session:
    def __init__(self):
        self.broadcasts = 0

    def broadcast_game_state(self):
        self.broadcasts += 1


class _Var:
    def __init__(self, v):
        self.v = v

    def get(self):
        return self.v


def _ui(mp=True, is_host=True, seat_to_peer=None):
    ps = [Player(i, f"P{i}", 1000) for i in range(3)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(5))
    e.button = 2                      # -> btn 0, sb 1, bb 2, first actor 0
    assert e.start_hand()
    calls = {"ai": [], "lock": 0, "unlock": 0, "showdown": 0}
    ui = SimpleNamespace(
        _mp_mode=mp, _mp_is_host=is_host,
        _mp_seat_to_peer=(seat_to_peer if seat_to_peer is not None
                          else {0: "me", 1: "peerB", 2: "peerC"}),
        _mp_session=_Session(), _mp_local_seat=0,
        engine=e, hero_seat=0, root=_Root(), delay=0,
        v_observe=_Var(False), v_bet=_Var(0),
        rng=random.Random(0), brain=SimpleNamespace(
            decide=lambda eng, i: ("call", 0)),
        _allin_announced=set(), calls=calls, hand_over=False,
    )
    noop = lambda *a, **k: None
    for name in ("flush_log", "redraw", "start_equity", "start_clock",
                 "stop_clock", "_record_vpip_pfr", "_show_emote",
                 "_mp_send_action"):
        setattr(ui, name, noop)
    ui._defer = lambda *a: False
    ui._maybe_fire_pre_action = lambda: False
    ui.lock = lambda: calls.__setitem__("lock", calls["lock"] + 1)
    ui.unlock = lambda: calls.__setitem__("unlock", calls["unlock"] + 1)
    ui.showdown = lambda **k: calls.__setitem__("showdown", calls["showdown"] + 1)
    ui.locked_runout = noop
    for name in ("loop", "ai_turn", "hero", "_forced", "_mp_peer_acted",
                 "_mp_is_remote_seat", "_mp_host_sync"):
        setattr(ui, name, types.MethodType(getattr(Holdem, name), ui))
    return ui


@pytest.fixture(autouse=True)
def _silence_audio(monkeypatch):
    monkeypatch.setattr(gui._audio, "play", lambda *a, **k: None)


def _scheduled_ai(ui):
    """Turn every scheduled callback that is an ai_turn lambda into its seat."""
    seats = []
    for fn in ui.root.scheduled:
        import inspect
        fn = inspect.unwrap(fn) if fn is not None else None
        if fn is not None and getattr(fn, "__defaults__", None) and \
                "ai_turn" in getattr(fn, "__code__", None).co_names:
            seats.append(fn.__defaults__[0])
    return seats


def _to_seat(ui, seat):
    """Advance the real engine (legally) until `seat` is to act."""
    e = ui.engine
    while e.actor != seat:
        e.act(e.actor, "call")
    ui.root.scheduled.clear()
    ui._mp_session.broadcasts = 0


# ------------------------------------------------------------ finding 3

def test_host_loop_waits_for_a_remote_human_instead_of_playing_it():
    ui = _ui()
    _to_seat(ui, 1)                                 # seat 1 = remote peerB
    ui.loop()
    assert _scheduled_ai(ui) == [], "the AI must not play a remote human"
    assert ui.engine.actor == 1                     # still waiting on peerB
    assert ui.calls["lock"] >= 1                    # host controls locked
    assert ui._mp_session.broadcasts >= 1           # peerB is told to act


def test_ai_turn_refuses_a_remote_seat_even_if_already_scheduled():
    ui = _ui()
    _to_seat(ui, 2)
    before = [(p.stack, p.bet, p.folded) for p in ui.engine.players]
    ui.ai_turn(2)                                   # stale callback fires
    assert ui.engine.actor == 2
    assert [(p.stack, p.bet, p.folded) for p in ui.engine.players] == before


def test_the_remote_peers_own_action_is_applied_and_advances():
    ui = _ui()
    _to_seat(ui, 1)
    ui.loop()                                       # host now waits on seat 1
    ui._mp_peer_acted(1, "call", 0)                 # peerB's real action
    assert ui.engine.actor == 2
    assert ui.engine.players[1].last_action in ("CALL 10", "CHECK")


def test_unmapped_filler_seats_are_still_played_by_the_ai():
    ui = _ui(seat_to_peer={0: "me", 1: "peerB", 2: ""})
    _to_seat(ui, 2)
    ui.loop()
    assert _scheduled_ai(ui) == [2]


def test_single_player_still_hands_every_other_seat_to_the_ai():
    ui = _ui(mp=False, seat_to_peer={})
    _to_seat(ui, 1)
    ui.loop()
    assert _scheduled_ai(ui) == [1]
    assert not ui._mp_is_remote_seat(1)


def test_illegal_peer_action_is_refused_without_raising():
    ui = _ui()
    _to_seat(ui, 1)
    before = [(p.stack, p.bet, p.folded) for p in ui.engine.players]
    ui._mp_peer_acted(1, "teleport", 0)             # garbage from a peer
    assert ui.engine.actor == 1
    assert [(p.stack, p.bet, p.folded) for p in ui.engine.players] == before
    assert ui._mp_session.broadcasts == 1           # resync published


# ------------------------------------------------------------ finding 4

def test_host_hero_action_is_broadcast():
    ui = _ui()
    _to_seat(ui, 0)
    ui.hero("call")
    assert ui.engine.actor == 1
    assert ui._mp_session.broadcasts == 1


def test_host_ai_action_is_broadcast():
    ui = _ui(seat_to_peer={0: "me", 1: "peerB", 2: ""})
    _to_seat(ui, 2)
    ui.ai_turn(2)
    assert ui.engine.actor != 2
    assert ui._mp_session.broadcasts == 1


def test_host_forced_action_is_broadcast():
    ui = _ui()
    _to_seat(ui, 0)
    ui._forced("fold")
    assert ui.engine.players[0].folded
    assert ui._mp_session.broadcasts == 1


def test_street_advance_is_broadcast():
    ui = _ui()
    e = ui.engine
    while e.actor is not None:                      # close preflop legally
        e.act(e.actor, "call")
    ui._mp_session.broadcasts = 0
    ui.loop()
    assert e.street == "flop"
    assert ui._mp_session.broadcasts == 1


def test_nothing_is_broadcast_outside_multiplayer_host_mode():
    for mp, host in ((False, True), (True, False)):
        ui = _ui(mp=mp, is_host=host, seat_to_peer={} if not mp else None)
        _to_seat(ui, 0)
        ui._mp_host_sync()
        assert ui._mp_session.broadcasts == 0
