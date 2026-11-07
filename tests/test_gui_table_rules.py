"""Negotiated multiplayer rules override conflicting local preferences."""
from types import SimpleNamespace
import pytest
from holdem import gui


def test_multiplayer_engine_and_rule_decisions_use_one_immutable_contract(monkeypatch):
    ui = gui.Holdem.__new__(gui.Holdem)
    ui._mp_mode, ui._mp_local_seat = True, 0
    ui._invalidate_equity = lambda: None
    advertised = dict(mode="Tournament", structure="Pot-Limit", bb_ante=True,
                      sb=25, bb=50, stack=1000, rit="Never", straddles=False,
                      rabbit=False, training_aids=False, level_minutes=7)
    ui._mp_session = SimpleNamespace(_last_table_settings=advertised,
          seat_order=["A", "B"], players={cid: SimpleNamespace(nickname=cid) for cid in ("A", "B")})
    monkeypatch.setattr(gui._notes, "update_nickname", lambda *args: None)
    constructed = []
    real_engine = gui.Engine
    class ReachedEngine(Exception): pass
    def capture(*args, **kwargs):
        constructed.append(real_engine(*args, **kwargs))
        raise ReachedEngine()
    monkeypatch.setattr(gui, "Engine", capture)
    with pytest.raises(ReachedEngine): ui._mp_new_game()
    engine = constructed[0]
    assert engine.structure == "Pot-Limit" and engine.bb_ante and engine.deal_sitting_out
    assert (engine.sb, engine.bb) == (25, 50)
    assert ui.table_rules is ui._mp_rules
    for key in advertised:
        local = SimpleNamespace(get=lambda: "conflicting preference")
        assert gui._game_rule(ui, key, local) == advertised[key]
    advertised["mode"] = "Cash"
    assert gui._game_rule(ui, "mode", SimpleNamespace(get=lambda: "Cash")) == "Tournament"
    with pytest.raises(TypeError): ui._mp_rules["mode"] = "Cash"


def test_solo_rules_still_use_local_preferences():
    ui = SimpleNamespace(_mp_mode=False)
    assert gui._game_rule(ui, "mode", SimpleNamespace(get=lambda: "Cash")) == "Cash"
