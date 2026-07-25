"""Player-facing poker information must be exact and JSON-ready."""
import json
import random

import pytest

from holdem import player_info
from holdem.engine import Card, Engine, Player, equity


def _table(n=3, stack=500):
    players = [Player(i, f"P{i}", stack) for i in range(n)]
    engine = Engine(players, sb=5, bb=10, structure="No-Limit")
    engine.start_hand()
    return engine


def _check_down(engine):
    while engine.street != "showdown":
        if engine.actor is None:
            engine.next_street()
        else:
            engine.act(engine.actor, "call")
    return engine.settle()


def test_multiway_board_tie_uses_actual_split_share():
    board = [Card(value, 3) for value in (10, 11, 12, 13, 14)]
    win, tie, share = equity(
        [Card(2, 0), Card(3, 1)],
        board,
        n_opp=3,
        sims=40,
        rng=random.Random(7),
    )
    assert win == 0
    assert tie == 1
    assert share == pytest.approx(0.25)


def test_made_hand_describes_board_pair_and_board_plays():
    hole = [Card(14, 1), Card(12, 1)]
    flop = [Card(13, 1), Card(4, 0), Card(13, 2)]
    made = player_info.made_hand_view(hole, flop)
    assert made["name"] == "One Pair"
    assert made["description"] == "Pair of Kings, Ace-Queen-Four kickers"
    assert made["board_plays"] is False

    royal_board = [Card(value, 3) for value in (10, 11, 12, 13, 14)]
    board_made = player_info.made_hand_view(hole, royal_board)
    assert board_made["description"] == "Royal Flush"
    assert board_made["board_plays"] is True


def test_turn_view_contains_complete_legal_decision_context():
    engine = _table()
    seat = engine.actor
    view = player_info.turn_view(engine, seat)
    legal = engine.legal(seat)

    assert view["state"] == "your_turn"
    assert view["actor"] == seat
    assert str(legal["to_call"]) in view["headline"]
    assert view["decision"]["to_call"] == legal["to_call"]
    assert view["decision"]["pot_now"] == engine.pot
    assert view["decision"]["pot_after_call"] == engine.pot + legal["to_call"]
    assert view["decision"]["min_raise_to"] == legal["min_to"]
    assert view["decision"]["max_raise_to"] == legal["max_to"]


def test_turn_view_never_leaves_a_previous_action_prompt_visible():
    engine = _table()
    seat = engine.actor
    engine.act(seat, "call")
    waiting = player_info.turn_view(engine, seat)
    assert waiting["state"] == "waiting"
    assert waiting["headline"].startswith("Waiting for ")
    assert "decision" not in waiting

    engine.players[seat].folded = True
    folded = player_info.turn_view(engine, seat)
    assert folded["state"] == "folded_waiting"
    assert folded["headline"] == "You folded | hand in progress"


def test_engine_events_are_sequenced_structured_and_json_safe():
    engine = _table()
    start_events = player_info.event_views(engine.public_events)
    assert start_events[0]["event"] == "hand_started"
    assert [event["seq"] for event in start_events] == list(
        range(1, len(start_events) + 1)
    )

    actor = engine.actor
    engine.act(actor, "call")
    action = player_info.event_views(engine.public_events)[-1]
    assert action["event"] == "action"
    assert action["seat"] == actor
    assert action["action"] == "call"
    assert action["pot_after"] == engine.pot
    json.dumps(action)


def test_settlement_summary_accounts_for_every_awarded_chip():
    engine = _table(3)
    starting_stacks = [player.stack + player.bet for player in engine.players]
    result = _check_down(engine)
    summary = player_info.settlement_view(
        engine, result, seat=0, starting_stacks=starting_stacks
    )

    assert summary["pots"]
    for pot in summary["pots"]:
        for award in pot["awards"]:
            assert sum(payout["amount"] for payout in award["payouts"]) == \
                award["amount"]
    assert summary["showdown"]
    assert all(
        hand["description"]
        for player in summary["showdown"]
        for hand in player["hands"]
    )
    json.dumps(summary)


def test_verification_labels_do_not_claim_audit_early():
    assert player_info.verification_view("dealing")["state"] == "in_progress"
    assert player_info.verification_view("betting")["state"] == "audit_pending"
    assert player_info.verification_view("settled")["state"] == "verified"
    voided = player_info.verification_view("void", "seat 2 sent a bad proof")
    assert voided["state"] == "voided"
    assert "seat 2" in voided["label"]


def test_eliminated_player_state_overrides_stale_hand_result():
    engine = _table()
    view = player_info.turn_view(
        engine,
        seat=0,
        phase="settled",
        result={"winners": [1]},
        eliminated=True,
    )
    assert view["state"] == "eliminated"
    assert view["headline"] == "You are out | table still playing"
