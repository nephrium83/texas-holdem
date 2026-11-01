"""A promoted peer never treats its lossy replica as authoritative
(audit finding 5).

_mp_on_host_changed used to wire the peer's engine -- rebuilt from
game_state by _mp_apply_state: public fields, stub hole cards, no deck, no
betting internals -- straight into the session as the authoritative host
engine, mid-hand. Now a mid-hand promotion voids the hand instead, and the
replica is only adopted between hands.
"""
import random
import sys
import types
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.engine import Engine, Player
from holdem.gui import Holdem


class _Session:
    def __init__(self):
        self._engine = None
        self.on_action = None
        self.broadcasts = 0

    def broadcast_game_state(self):
        self.broadcasts += 1


def _peer(street):
    ps = [Player(i, f"P{i}", 1000) for i in range(3)]
    e = Engine(ps, 10, 20, "No-Limit", random.Random(1))
    e.button = 2
    e.start_hand()
    if street == "idle":
        e.street = "idle"
        e.actor = None
    status = {}
    ui = SimpleNamespace(
        _mp_mode=True, _mp_is_host=False, _mp_session=_Session(),
        engine=e, hand_over=(street == "idle"), _host_bar_built=True,
        root=SimpleNamespace(after=lambda *a, **k: None),
        l_status=SimpleNamespace(config=lambda **k: status.update(k)),
        status=status, locked=[],
    )
    ui._show_banner = lambda *a, **k: None
    ui.lock = lambda: ui.locked.append(True)
    noop = lambda *a, **k: None
    for name in ("flush_log", "redraw", "start_equity", "locked_runout",
                 "showdown"):
        setattr(ui, name, noop)
    ui._defer = lambda *a: False
    for name in ("_mp_on_host_changed", "_mp_replica_mid_hand",
                 "_mp_void_promoted_hand", "_mp_broadcast_state",
                 "_mp_peer_acted", "_mp_host_sync", "loop"):
        setattr(ui, name, types.MethodType(getattr(Holdem, name), ui))
    return ui


def test_mid_hand_promotion_voids_instead_of_adopting_the_replica():
    ui = _peer("preflop")
    assert ui._mp_replica_mid_hand()
    ui._mp_on_host_changed(True)
    s = ui._mp_session
    assert s._engine is None, "the lossy replica must not become authoritative"
    assert s.on_action is None, "no peer action may be applied against it"
    assert s.broadcasts == 0, "nothing is published from a lossy replica"
    assert ui.engine.actor is None and ui.hand_over
    assert ui.locked
    assert "void" in ui.status.get("text", "")
    assert ui._mp_is_host                       # still the new host


def test_between_hands_promotion_adopts_the_replica_for_the_next_deal():
    ui = _peer("idle")
    assert not ui._mp_replica_mid_hand()
    ui._mp_on_host_changed(True)
    s = ui._mp_session
    assert s._engine is ui.engine
    assert s.on_action is not None
    assert s.broadcasts == 1


def test_not_promoted_changes_nothing():
    ui = _peer("preflop")
    ui._mp_on_host_changed(False)
    assert ui._mp_session._engine is None
    assert not ui._mp_is_host
    assert ui.engine.actor is not None


def test_loop_after_a_voided_promotion_never_advances_the_hand():
    """Found in review: after the void the new host has _mp_is_host=True and
    actor None, which loop() would read as "betting round closed" and use to
    deal the next street of the voided hand onto the replica's stub cards.
    loop() must refuse to drive a hand that is over."""
    ui = _peer("preflop")
    ui._mp_on_host_changed(True)
    e = ui.engine
    street, board = e.street, list(e.board)
    ui.loop()
    assert e.street == street and e.board == board
    assert ui._mp_session.broadcasts == 0
