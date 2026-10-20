"""Source-backed famous hands, replayed through production betting replicas."""
from copy import deepcopy
from itertools import combinations
import json
from pathlib import Path

import pytest

from famous_hands.replay import AnteRuleMismatch, replay_case
from holdem.engine import Card, FULL_DECK, evaluate


CASES = json.loads((Path(__file__).parent / "famous_hands/cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", [
    pytest.param(case, id=case["id"], marks=pytest.mark.xfail(
        strict=True, raises=AnteRuleMismatch,
        reason="Saved house ante rule differs from WSOP; payout-rule decision pending"))
    if case["id"] == "weinman_jones_2023_final" else pytest.param(case, id=case["id"])
    for case in CASES
])
def test_sourced_hand_replay(case):
    report = replay_case(case)
    assert report["passed"]


def test_rounders_final_cannot_lose_to_any_compatible_hidden_hand():
    def cards(labels):
        return [Card("23456789TJQKA".index(label[0]) + 2,
                     "cdhs".index(label[1])) for label in labels.split()]

    hero = cards("8s 9s")
    board = cards("6d 7s Th 2c As")
    known = {(card.v, card.s) for card in hero + board}
    remaining = [card for card in FULL_DECK if (card.v, card.s) not in known]
    hero_score = evaluate(hero + board)
    outcomes = {"wins": 0, "ties": 0, "losses": 0}
    for villain in combinations(remaining, 2):
        score = evaluate(list(villain) + board)
        result = "wins" if hero_score > score else "ties" if hero_score == score else "losses"
        outcomes[result] += 1
    assert outcomes == {"wins": 981, "ties": 9, "losses": 0}


def test_harness_rejects_inaccurate_bet_instead_of_accepting_engine_clamp():
    case = deepcopy(CASES[0])
    case["streets"][0]["actions"][0] = [0, "raise", 40001]
    with pytest.raises(AssertionError, match="raise"):
        replay_case(case)


def test_harness_rejects_duplicate_sourced_cards():
    case = deepcopy(CASES[0])
    case["holes"]["1"][0] = case["holes"]["0"][0]
    with pytest.raises(AssertionError, match="duplicate"):
        replay_case(case)


@pytest.mark.parametrize("field,value", [
    ("winners", [0]), ("pots", [1]), ("refund", [0, 1]), ("stacks", [1, 1]),
])
def test_known_ante_gap_cannot_hide_a_corrupted_fixture(field, value):
    case = deepcopy(next(case for case in CASES if case["id"] == "weinman_jones_2023_final"))
    case["expected"][field] = value
    with pytest.raises(AssertionError, match="fixture") as error:
        replay_case(case)
    assert not isinstance(error.value, AnteRuleMismatch)
