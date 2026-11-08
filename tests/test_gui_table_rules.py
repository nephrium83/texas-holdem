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
                      rabbit=False, training_aids=False, level_minutes=7, bank_start=12, bank_topup=7, bank_cap=19, clock_base=44)
    ui._mp_session = SimpleNamespace(_last_table_settings=advertised,
          seat_order=["A", "B"], players={cid: SimpleNamespace(nickname=cid) for cid in ("A", "B")})
    monkeypatch.setattr(gui._notes, "update_nickname", lambda *args: None)
    constructed = []
    real_engine = gui.Engine
    class ReachedEngine(Exception): pass
    def capture(*args, **kwargs):
        constructed.append(real_engine(*args, **kwargs))
        return constructed[-1]
    monkeypatch.setattr(gui, "Engine", capture)
    ui.log = SimpleNamespace(config=lambda **kwargs: None, delete=lambda *args: None)
    ui.stop_clock = lambda: None
    ui._retheme = lambda: (_ for _ in ()).throw(ReachedEngine())
    with pytest.raises(ReachedEngine): ui._mp_new_game()
    assert ui.bank == 12
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


def test_open_settings_cannot_amend_admitted_contract():
    import ast
    import inspect
    from types import MappingProxyType
    ui = SimpleNamespace(_mp_mode=True, table_rules=MappingProxyType({"mode": "Tournament"}))
    contract = ui.table_rules
    node = next(node for node in ast.walk(ast.parse(inspect.getsource(gui)))
                if isinstance(node, ast.FunctionDef) and node.name == "amend")
    namespace = {"self": ui}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<actual settings amend>", "exec"), namespace)
    namespace["amend"]()
    assert ui.table_rules is contract


def test_admitted_clock_and_bb_bank_topup_use_table_values(monkeypatch):
    from test_gui_lifecycle import clock_ui
    monkeypatch.setattr(gui.time, "time", lambda: 100)
    monkeypatch.setattr(gui._audio, "play", lambda *args: None)
    ui = clock_ui("off")
    ui._mp_rules = dict(clock_base=44, bank_topup=7, bank_cap=19)
    ui.engine.players = [SimpleNamespace(sitting_out=False, in_seat=True)]
    ui.v_clock = SimpleNamespace(get=lambda: True)
    ui.v_observe = SimpleNamespace(get=lambda: False)
    ui.start_clock()
    assert ui.clock_until == 144
    ui.bank = 18
    ui.engine.bb_i = 0
    ui.engine.sb, ui.engine.bb, ui.engine.bb_ante = 25, 50, False
    ui.hand_logger = SimpleNamespace(on_hand_start=lambda *args: None)
    ui.session_stats = SimpleNamespace(record_hand_start=lambda *args: None)
    ui.update_xp = lambda **kwargs: None
    ui.flush_log = ui.loop = ui._mp_after_deal = lambda: None
    ui.l_blinds = ui.b_next = ui.b_add = SimpleNamespace(config=lambda **kwargs: None)
    ui._post_start_hand()
    assert ui.bank == 19
