"""Smoke success requires settlement progress or a proven tournament finish."""
from types import SimpleNamespace
import subprocess
import pytest
import gui_smoke as smoke


def app(stacks, *, hand_no=0, game_over=False):
    players = [SimpleNamespace(stack=stack, total=0) for stack in stacks]
    return SimpleNamespace(engine=SimpleNamespace(players=players, hand_no=hand_no),
                           game_over=game_over, hand_over=True)


@pytest.mark.parametrize("hand_no", [0, smoke.HANDS])
def test_started_hands_or_false_game_over_are_not_completion(hand_no):
    ui = app([smoke.STACK] * smoke.PLAYERS, hand_no=hand_no, game_over=True)
    assert "insufficient completed" in smoke._completion_error(ui, set())


def test_only_legitimate_completed_tournament_can_finish_before_target():
    ui = app([smoke.PLAYERS * smoke.STACK, 0, 0, 0, 0, 0], hand_no=3, game_over=True)
    assert smoke._completion_error(ui, {1, 2, 3}) is None
    ui.engine.players[0].stack -= 1
    assert "chip-count drift" in smoke._completion_error(ui, {1, 2, 3})


def test_parent_deadline_cannot_report_success(monkeypatch):
    def blocked(argv, **kwargs):
        assert kwargs["timeout"] == smoke.TOTAL_TIMEOUT
        assert kwargs["env"]["HOLDEM_CONFIG_DIR"]
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
    monkeypatch.setattr(smoke.subprocess, "run", blocked)
    assert smoke.main() == 1
