"""Practice wallet conservation, rollback and table-lifetime boundaries."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest
from holdem import gui, settings as cfg
from holdem.engine import Engine, Player


def table(stack=100):
    return Engine([Player(0, "You", stack), Player(1, "Bot", stack)], sb=1, bb=2)


def app():
    ui = gui.Holdem.__new__(gui.Holdem)
    ui._mp_mode = False
    ui._mp_local_seat = 0
    ui._cash_engine = None
    ui._cash_seat = 0
    ui._game_epoch = 0
    ui.engine = None
    ui.hand_over = True
    ui.eq_gen = 0
    ui.stop_clock = lambda: None
    ui.table_rules = {"buyin_max_bb": 100}
    return ui


def seat(ui, engine, *, cash=True):
    assert ui._fund_table(engine, cash)
    ui.engine = engine
    ui.game_over = False
    return engine


@pytest.fixture(autouse=True)
def wallet(monkeypatch):
    assert cfg.set("bankroll", 1000)
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: None)


def test_losses_wins_topups_and_exit_conserve_practice_funds():
    ui = app()
    engine = seat(ui, table())
    assert cfg.get("bankroll") == 900
    engine.players[0].stack -= 50
    engine.players[1].stack += 50  # lost chips remain on the table
    assert ui._buy_practice_chips(50)
    assert cfg.get("bankroll") == 850 and engine.players[0].stack == 100
    engine.players[0].stack += 60
    engine.players[1].stack -= 60  # a win changes the table, never the wallet
    assert cfg.get("bankroll") == 850
    assert ui._release_practice_chips()
    assert cfg.get("bankroll") == 1010
    assert engine.players[0].stack == 0
    assert ui._release_practice_chips()  # repeated departure cannot double-credit
    assert cfg.get("bankroll") == 1010


def test_felted_rebuy_is_debited_and_unaffordable_purchase_is_inert():
    ui = app()
    engine = seat(ui, table())
    engine.players[0].stack = 0
    assert ui._buy_practice_chips(100)
    assert cfg.get("bankroll") == 800 and engine.players[0].stack == 100
    assert cfg.set("bankroll", 10)
    assert not ui._buy_practice_chips(50)
    assert cfg.get("bankroll") == 10 and engine.players[0].stack == 100


def test_switch_uses_one_net_transfer_and_failure_keeps_old_reservation():
    ui = app()
    old = seat(ui, table())
    replacement = table(200)
    seat(ui, replacement)
    assert cfg.get("bankroll") == 800
    assert old.players[0].stack == 0 and replacement.players[0].stack == 200
    assert cfg.set("bankroll", 1)
    before = cfg.config_path().read_bytes()
    assert not ui._fund_table(table(500), True)
    assert ui._cash_engine is replacement and replacement.players[0].stack == 200
    assert cfg.config_path().read_bytes() == before


@pytest.mark.parametrize("multiplayer,cash", [(True, True), (False, False)])
def test_switch_to_unfunded_mode_returns_only_old_practice_stack(multiplayer, cash):
    ui = app()
    old = seat(ui, table())
    ui._mp_mode = multiplayer
    staged = table(5000)
    assert ui._fund_table(staged, cash)
    ui.engine = staged
    assert cfg.get("bankroll") == 1000 and old.players[0].stack == 0
    assert ui._cash_engine is None
    assert not ui._buy_practice_chips(20)
    assert ui._release_practice_chips()
    assert cfg.get("bankroll") == 1000 and staged.players[0].stack == 5000


@pytest.mark.parametrize("bad", [-1, 100000001, "1000", None, True, 1.5])
def test_malformed_raw_balance_is_refused_without_normalization(bad):
    path = cfg.config_path()
    raw = json.loads(path.read_text())
    raw["client"]["bankroll"] = bad
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    player = table().players[0]
    ok, _ = gui._bankroll_transfer(-10, [(player, 100, 110)])
    assert not ok and player.stack == 100 and path.read_bytes() == before


def test_failed_publication_rolls_back_wallet_and_table_stack(monkeypatch):
    ui = app()
    engine = seat(ui, table())
    before = cfg.config_path().read_bytes()
    monkeypatch.setattr(cfg, "_write_json", lambda *a: (_ for _ in ()).throw(OSError("disk failure")))
    assert not ui._buy_practice_chips(50)
    assert engine.players[0].stack == 100
    assert ui._cash_engine is engine and cfg.config_path().read_bytes() == before
    assert not ui._release_practice_chips()
    assert engine.players[0].stack == 100 and ui._cash_engine is engine


def test_unlock_error_after_publication_does_not_undo_committed_money(monkeypatch):
    real_lock = cfg._json_lock
    @contextmanager
    def failed_unlock(path):
        with real_lock(path):
            yield
        raise OSError("unlock reported failure after publication")
    monkeypatch.setattr(cfg, "_json_lock", failed_unlock)
    player = table().players[0]
    ok, _ = gui._bankroll_transfer(-10, [(player, 100, 110)])
    assert ok and player.stack == 110 and cfg.get("bankroll") == 990


def test_bonus_is_once_and_cannot_overwrite_concurrent_purchase():
    def bonus(): return gui._bankroll_transfer(bonus_date="2099-01-01")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: bonus(), range(8)))
    assert all(ok for ok, delta in results)
    assert sum(delta for ok, delta in results) == 500
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(gui._bankroll_transfer, -100)
        b = pool.submit(gui._bankroll_transfer, bonus_date="2099-01-02")
        assert a.result()[0] and b.result()[0]
    assert cfg.get("bankroll") == 1900


def test_leaving_invalidates_already_scheduled_gameplay():
    ui = app()
    pending, fired = [], []
    ui.root = SimpleNamespace(after=lambda delay, fn: pending.append(fn))
    seat(ui, table())
    gui._game_after(ui, 1, lambda: fired.append("old table"))
    assert ui._release_practice_chips()
    pending.pop()()
    assert not fired
    gui._game_after(ui, 1, lambda: fired.append("current table"))
    pending.pop()()
    assert fired == ["current table"]


def test_failed_quit_refund_keeps_window_and_reservation(monkeypatch):
    ui = app()
    engine = seat(ui, table())
    destroyed = []
    ui.root = SimpleNamespace(destroy=lambda: destroyed.append(True))
    ui.settings_win = None
    ui.save_config = lambda: None
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(cfg, "_write_json", lambda *a: (_ for _ in ()).throw(OSError("disk failure")))
    ui.quit_to_desktop()
    assert not destroyed and ui._cash_engine is engine and engine.players[0].stack == 100


def test_multiplayer_cashout_and_departure_do_not_touch_practice_wallet(monkeypatch, tmp_path):
    from test_gui_cashout_receipts import cashout_ui
    ui, engine, history, text = cashout_ui(monkeypatch, tmp_path, [50, 300], 0, .8)
    before = cfg.get("bankroll")
    assert ui._maybe_offer_cashout(engine)
    assert cfg.get("bankroll") == before
    assert ui._release_practice_chips()
    assert cfg.get("bankroll") == before


def test_multiplayer_topup_is_rejected_before_showing_a_dialog(monkeypatch):
    ui = app()
    ui._mp_mode = True
    ui.engine = table()
    monkeypatch.setattr(gui.simpledialog, "askinteger", lambda *a, **k: pytest.fail("MP topup offered"))
    ui.add_chips_dialog()
    assert cfg.get("bankroll") == 1000 and ui.engine.players[0].stack == 100


@pytest.mark.parametrize("multiplayer", [False, True])
def test_real_showdown_winnings_remain_on_table_until_solo_departure(monkeypatch, tmp_path, multiplayer):
    from holdem.hand_history import HandLogger
    ui = app()
    ui._mp_mode = multiplayer
    ui._mp_is_host = False
    engine = seat(ui, table())
    before = cfg.get("bankroll")
    assert engine.start_hand()
    ui.hand_logger = HandLogger(tmp_path / "showdown-history.jsonl")
    ui.hand_logger.on_hand_start(engine.players, engine)
    engine.players[1].folded = True
    engine.actor = None
    engine.need_to_act.clear()
    ui._defer = lambda fn: False
    ui.lock = ui.flush_log = ui.redraw = lambda: None
    ui.session_stats = SimpleNamespace(record_win=lambda seat: None)
    ui._hand_start_stacks = [100, 100]
    ui.highlight = set()
    ui._last_hero_eq = 0
    ui.update_xp = lambda **kwargs: None
    ui.set_showdown_text = lambda text: None
    ui.v_mode = SimpleNamespace(get=lambda: "Cash")
    ui.v_reveal = SimpleNamespace(get=lambda: "Winner only")
    ui.v_rabbit = ui.v_auto = ui.v_observe = SimpleNamespace(get=lambda: False)
    ui.l_status = ui.l_hint = ui.b_rabbit = ui.b_add = ui.b_next = SimpleNamespace(config=lambda **kwargs: None)
    monkeypatch.setattr(gui._audio, "play", lambda *args: None)
    ui.showdown()
    assert engine.players[0].stack == 101
    assert cfg.get("bankroll") == before, "showdown double-counted table chips as wallet funds"
    assert ui._release_practice_chips()
    assert cfg.get("bankroll") == (1000 if multiplayer else 1001)


def test_preference_save_cannot_launder_a_corrupt_wallet():
    path = cfg.config_path()
    raw = json.loads(path.read_text())
    raw["client"]["bankroll"] = "corrupt"
    path.write_text(json.dumps(raw))
    assert cfg.set("theme", "Classic Felt")
    assert json.loads(path.read_text())["client"]["bankroll"] == "corrupt"
    assert not gui._bankroll_transfer(-100)[0]


def test_real_new_game_validates_wallet_before_preference_save():
    import random
    ui = app()
    rules = dict(mode="Cash", players=2, sb=1, bb=2, stack=100,
                 buyin_min_bb=10, buyin_max_bb=100)
    ui._bucket = lambda scope: rules if scope == cfg.TABLE_RULE else {"theme": "Cyberpunk"}
    ui.v_theme = SimpleNamespace(get=lambda: "Cyberpunk")
    ui.v_level = SimpleNamespace(get=lambda: 2)
    ui.v_chaos = ui.v_ante = SimpleNamespace(get=lambda: False)
    ui.v_struct = SimpleNamespace(get=lambda: "No-Limit")
    ui.rng = random.Random(0)
    path = cfg.config_path()
    raw = json.loads(path.read_text())
    raw["client"]["bankroll"] = "corrupt"
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    ui.new_game()
    assert ui.engine is None and ui._cash_engine is None
    assert path.read_bytes() == before, "new_game sanitized the wallet before funding"


def test_post_commit_ui_failure_retains_refundable_ownership():
    ui = app()
    engine = table()
    def broken_cleanup(): raise RuntimeError("unexpected teardown defect")
    ui._stop_gameplay = broken_cleanup
    with pytest.raises(RuntimeError, match="teardown defect"):
        ui._fund_table(engine, True)
    assert cfg.get("bankroll") == 900 and ui._cash_engine is engine
    assert ui._release_practice_chips(stop=False)
    assert cfg.get("bankroll") == 1000


@pytest.mark.parametrize("key,bad", [("player_level", 9999), ("player_level", "3"),
                                     ("last_daily_bonus_date", None), ("last_daily_bonus_date", "junk")])
def test_unrelated_save_preserves_invalid_reward_metadata_and_bonus_refuses(key, bad):
    path = cfg.config_path()
    raw = json.loads(path.read_text())
    raw["client"][key] = bad
    path.write_text(json.dumps(raw))
    assert cfg.set("theme", "Classic Felt")
    assert json.loads(path.read_text())["client"][key] == bad
    before = path.read_bytes()
    assert not gui._bankroll_transfer(bonus_date="2099-01-01")[0]
    assert path.read_bytes() == before


def test_closed_clock_widget_cannot_block_a_committed_funding_or_refund():
    ui = app()
    def closed_widget(): raise gui.tk.TclError("stale clock widget")
    ui.stop_clock = closed_widget
    engine = seat(ui, table())
    assert ui._cash_engine is engine and cfg.get("bankroll") == 900
    assert ui._release_practice_chips()
    assert cfg.get("bankroll") == 1000 and ui._cash_engine is None
