"""History records must preserve engine seat IDs, chip amounts and boards."""
import json
import random

import pytest

from holdem.engine import Engine, Player
from holdem.hand_history import HandLogger, HandRecord


def test_sparse_seat_names_and_legacy_records_do_not_confuse_offsets():
    record = HandRecord("id", 0, [{"seat": 2, "name": "Left"}, {"seat": 5, "name": "Right"}],
                        [], [], {5: ["A♠", "A♥"]}, {5}, 100)
    assert record.winner_names == "Right"
    assert record.seat_name(5) == "Right"
    legacy = HandRecord("old", 0, [{"name": "Left"}, {"name": "Right"}], [], [], {}, {1}, 100)
    assert legacy.winner_names == "Right"


@pytest.mark.parametrize("ante,straddle", [(False, False), (True, False), (False, True), (True, True)])
def test_starting_stacks_do_not_double_count_live_posts(tmp_path, ante, straddle):
    engine = Engine([Player(i, f"P{i}", 500) for i in range(3)], 5, 10, bb_ante=ante)
    assert engine.start_hand(straddle_fn=(lambda seat: True) if straddle else None)
    logger = HandLogger(tmp_path / "history.jsonl")
    logger.on_hand_start(engine.players, engine)
    assert [seat["stack_start"] for seat in logger._current.seats] == [500, 500, 500]
    assert sum(seat["stack_start"] for seat in logger._current.seats) == sum(p.stack for p in engine.players) + engine.pot


def test_structured_events_preserve_duplicate_names_actions_and_one_copy_of_each_board_card(tmp_path):
    players = [Player(i, "Alex", 500 if i in (2, 5) else 0) for i in range(6)]
    engine = Engine(players, 5, 10, rng=random.Random(7))
    assert engine.start_hand()
    path = tmp_path / "history.jsonl"
    logger = HandLogger(path)
    logger.on_hand_start(players, engine)
    assert [seat["seat"] for seat in logger._current.seats] == [2, 5]
    while engine.actor is not None:
        engine.act(engine.actor, "call")
    engine.next_street()
    raiser = engine.actor
    engine.act(raiser, "raise", 60)
    logger.feed([("bet", "localized text unrelated to any player name")])
    while engine.street != "showdown":
        if engine.actor is None:
            engine.next_street()
        else:
            engine.act(engine.actor, "call")
    result = engine.settle()
    logger.on_settle(result, engine)  # flush events even without a final feed
    record = logger.last_n()[0]
    actions = [event for event in engine.public_events if event["event"] == "action"]
    assert [(a["seat"], a["action_type"], a["amount"], a["to"], a["street"]) for a in record.actions] == [
        (a["seat"], a["action"], a["amount"], a["to"], a["street"]) for a in actions]
    opening = next(a for a in record.actions if a["action_type"] == "bet")
    assert (opening["seat"], opening["amount"], opening["to"]) == (raiser, 60, 60)
    assert [len(street) for street in record.community] == [3, 1, 1]
    assert [card for street in record.community for card in street] == [str(c) for c in engine.board]
    assert record.boards == {1: [str(c) for c in engine.board]}
    assert record.winners == result["winners"]
    assert record.pot_final == sum(pot["amount"] for pot in result["pots"])
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert [seat["seat"] for seat in persisted["seats"]] == [2, 5]
    assert persisted["community"] == record.community
    assert persisted["boards"]["1"] == record.boards[1]
    logger.feed(engine.events)
    assert len(logger.records) == 1


def test_run_it_twice_keeps_boards_separate(tmp_path):
    engine = Engine([Player(i, f"P{i}", 100) for i in range(2)], 5, 10, rng=random.Random(4))
    assert engine.start_hand()
    logger = HandLogger(tmp_path / "history.jsonl")
    logger.on_hand_start(engine.players, engine)
    engine.act(engine.actor, "raise", 100)
    engine.act(engine.actor, "call")
    result = engine.settle(runs=2)
    logger.on_settle(result, engine)
    record = logger.last_n()[0]
    assert record.boards == {1: [str(c) for c in engine.board], 2: [str(c) for c in engine.board2]}
    assert record.community == [record.boards[1][:3], record.boards[1][3:4], record.boards[1][4:5]]
