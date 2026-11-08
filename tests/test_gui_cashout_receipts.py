"""Real cashout payouts agree with table, history and every seat's display."""
import json
from types import SimpleNamespace
import pytest
from holdem import gui, engine as engine_module, player_info
from holdem.engine import Engine, Player, Card
from holdem.hand_history import HandLogger, cashout_history_lines


@pytest.mark.parametrize("contributions,hero,equity,expected,refund", [
    ([100, 100], 0, .8, {0: 158, 1: 42}, None),
    ([50, 300], 0, .8, {0: 79, 1: 21}, (1, 250)),
    ([50, 120, 300], 1, .6, {0: 31, 1: 172, 2: 87}, (2, 180)),
])
def test_actual_cashout_records_the_exact_aggregate_distribution(
        monkeypatch, tmp_path, contributions, hero, equity, expected, refund):
    ui, engine, history, text = cashout_ui(monkeypatch, tmp_path, contributions, hero, equity)
    total_before = sum(p.stack + p.total for p in engine.players)
    assert ui._maybe_offer_cashout(engine)
    receipt = ui.result["cashout"]
    assert receipt["payouts"] == expected
    assert receipt["refund"] == refund
    assert receipt["total_pot"] == sum(expected.values())
    assert receipt["original_pot"] == total_before
    assert set(ui.result["winners"]) == set(expected)
    assert ui.result["pots"] == ui.result["runs"] == []
    assert sum(p.stack + p.total for p in engine.players) == total_before
    for player in engine.players:
        returned = refund[1] if refund and player.idx == refund[0] else 0
        assert player.stack == expected[player.idx] + returned
        assert player.won == expected[player.idx]
        turn = player_info.turn_view(engine, player.idx, phase="settled", result=ui.result)
        summary = player_info.settlement_view(engine, ui.result, player.idx,
                                             starting_stacks=contributions)
        assert "Cashout distribution" in turn["headline"]
        assert summary["outcome"] == "cashout"
        assert summary["pots"] == summary["showdown"] == []
        assert {p["seat"]: p["amount"] for p in summary["payouts"]} == expected
        assert summary["total_pot"] == sum(expected.values())
        assert "won" not in turn["headline"].lower()
    record = history.last_n()[0]
    assert record.pot_final == sum(expected.values())
    assert record.cashout == receipt
    rendered = "\n".join(cashout_history_lines(record))
    assert "Cashout distribution" in rendered and "Winner:" not in rendered
    for seat, amount in expected.items(): assert f"P{seat} received {amount}" in rendered
    persisted = json.loads(history._history_path.read_text().splitlines()[-1])
    assert persisted["cashout"]["payouts"] == {str(k): v for k, v in expected.items()}
    assert persisted["pot_final"] == sum(expected.values())
    assert "Cashout distribution" in text[-1]
    assert "Main pot" not in text[-1]


def cashout_ui(monkeypatch, tmp_path, contributions, hero, equity):
    players = [Player(i, f"P{i}", 0) for i in range(len(contributions))]
    engine = Engine(players, sb=1, bb=2)
    engine.hand_no = 1
    engine.street = "turn"
    for player, amount in zip(players, contributions):
        player.in_seat = player.all_in = True
        player.total_live = amount
        player.hole = [Card(14 - player.idx, 0), Card(14 - player.idx, 1)]
        player.won = 999  # stale payouts must not survive settlement
    history = HandLogger(tmp_path / "cashout-history.jsonl")
    history.on_hand_start(players, engine)
    monkeypatch.setattr(engine_module, "equity", lambda *args: (equity, 0, equity))
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *args, **kwargs: True)
    ui = gui.Holdem.__new__(gui.Holdem)
    ui._mp_mode, ui._mp_local_seat = True, hero
    ui._cashout_offered = False
    ui.v_observe = ui.v_auto = SimpleNamespace(get=lambda: False)
    ui.l_status = ui.l_hint = ui.b_next = SimpleNamespace(config=lambda **kwargs: None)
    ui.root = SimpleNamespace(after=lambda *args: None)
    ui.hand_logger = history
    ui._hand_start_stacks = contributions
    ui.say = ui.redraw = lambda *args: None
    text = []
    ui.set_showdown_text = text.append
    return ui, engine, history, text


def test_cashout_refuses_a_contested_nonhero_sidepot_without_mutation(monkeypatch, tmp_path):
    ui, engine, history, text = cashout_ui(monkeypatch, tmp_path, [50, 120, 300], 0, .8)
    before = [(p.stack, p.total, p.won) for p in engine.players]
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *args: pytest.fail("ineligible cashout offered"))
    assert not ui._maybe_offer_cashout(engine)
    assert [(p.stack, p.total, p.won) for p in engine.players] == before
    assert not history.last_n() and not text and not ui._cashout_offered


def test_nonconserving_receipt_is_rejected_before_settlement():
    with pytest.raises(ValueError, match="conserve"):
        player_info.cashout_receipt(dict(payouts={0: 10}, refund=None, eligible_pot=20,
                                        gross=10, net=10, fee=0), 20, 0, .5)
