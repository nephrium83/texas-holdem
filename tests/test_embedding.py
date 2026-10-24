"""Public embedding behavior, using real rules and no desktop resources."""
from copy import deepcopy
import json
import random

import pytest

from holdem.embedding import API_VERSION, PlayerSeat, PokerTable, TableConfig
from holdem.player_info import card_str


def table(*, instance="visit-1", chips=(1000, 1000), rng=None, config=None):
    return PokerTable(instance, [PlayerSeat(f"p{i}", f"Guest {i}", stack)
                                for i, stack in enumerate(chips)],
                      rng=rng or random.Random(17), config=config)


def act_current(game, command="check_call", payload=None):
    snap = game.view("p0")
    actor = snap["seats"][snap["action_on"]]["player_id"]
    return game.command(actor, command, payload)


def finish(game):
    for _ in range(100):
        if game.phase == "settled":
            return game.hand_result()
        act_current(game)
    pytest.fail("hand did not finish within 100 legal actions")


class FixedCards(random.Random):
    """Host-supplied test deck, without replacing poker rules or private state."""

    def __init__(self, first):
        super().__init__(1)
        self.first = first

    def shuffle(self, cards):
        lookup = {card_str(card): card for card in cards}
        cards[:] = ([card for card in cards if card_str(card) not in self.first]
                    + [lookup[key] for key in reversed(self.first)])


def test_public_table_runs_all_streets_and_closes_with_owned_cashout():
    game = table()
    assert game.phase == "lobby"
    assert game.result() is game.hand_result() is None
    assert "legal" not in game.view("p0")["you"]
    game.start_hand()
    result = finish(game)
    view = game.view("p0")
    assert len(view["board"]) == 5
    assert view["api_version"] == API_VERSION == 1
    assert view["authority_mode"] == "trusted_host"
    assert sum(result["net_chips"].values()) == 0
    assert sum(result["final_chips"].values()) == 2000
    assert view["action_on"] == view["turn"]["actor"] == -1
    assert "legal" not in view["you"]
    assert any(event.get("action") == "check" for event in view["events"])
    assert len({event["event_id"] for event in view["events"]}) == len(view["events"])
    json.dumps(view)
    receipt = game.close()
    assert receipt["result_id"] == "visit-1/close"
    assert receipt["initial_chips"] == {"p0": 1000, "p1": 1000}
    assert receipt["final_chips"] == result["final_chips"]
    receipt["final_chips"]["p0"] += 999
    result["initial_chips"]["p0"] = 0
    view["seats"][0]["stack"] = -1
    assert game.result() == game.close()
    assert sum(game.close()["final_chips"].values()) == 2000
    assert game.hand_result()["initial_chips"]["p0"] == 1000
    assert game.view("p0")["seats"][0]["stack"] >= 0
    assert game.view("p0")["turn"]["state"] == "table_closed"
    with pytest.raises(ValueError):
        game.start_hand()


@pytest.mark.parametrize("player,command,payload", [
    ("outsider", "fold", None), ("p1", "fold", None),
    ("p0", "unknown", None), ("p0", "raise_to", {"amount": True}),
    ("p0", "raise_to", {"amount": "100"}),
    ("p0", "raise_to", {"amount": 99}),
    ("p0", "raise_to", {"amount": 1001}),
    ("p0", "raise_to", {}), ("p0", "fold", {"amount": 1}),
    ("p0", "check_call", []), ("p0", [], None),
])
def test_invalid_external_actions_are_rejected_without_mutation(player, command, payload):
    game = table()
    game.start_hand()
    before = [game.view("p0"), game.view("p1")]
    with pytest.raises(ValueError):
        game.command(player, command, payload)
    assert before == [game.view("p0"), game.view("p1")]


def test_close_refuses_unsettled_hand_and_repeated_results_do_not_repeat_payout():
    game = table()
    game.start_hand()
    with pytest.raises(ValueError, match="Finish"):
        game.close()
    assert game.result() is None
    act_current(game, "fold")
    result = game.hand_result()
    for _ in range(3):
        assert game.hand_result() == result
        with pytest.raises(ValueError):
            game.command("p0", "check_call")
    assert sum(game.close()["final_chips"].values()) == 2000


def test_private_cards_survive_fold_and_losing_mucked_hand_stays_hidden():
    # Seat 1 shows aces first; the losing kings in seat 0 are mucked.
    rng = FixedCards(["As", "Ks", "Ah", "Kd", "2c", "4d", "7h", "9s", "Jc"])
    game = table(rng=rng)
    game.start_hand()
    assert set(game.view("p0")["you"]["hole"]) == {"Ks", "Kd"}
    assert all("hole" not in seat for seat in game.view("p1")["seats"])
    finish(game)
    opponent = game.view("p1")
    assert [shown["player_id"] for shown in opponent["showdown"]] == ["p1"]
    assert not {"Ks", "Kd"} & set(_strings(opponent))
    assert game.hand_result()["winner_ids"] == ["p1"]
    folded = table(instance="folded")
    folded.start_hand()
    hidden = set(folded.view("p0")["you"]["hole"])
    folded.command("p0", "fold")
    assert folded.view("p1")["showdown"] == []
    assert not hidden & set(_strings(folded.view("p1")))


def _strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def test_side_pots_all_in_and_busted_seat_use_existing_rules():
    # Deal left of button: seat 1, seat 2, seat 0. AA wins main;
    # KK wins the side pot over QQ. QQ retains its uncommitted 100.
    rng = FixedCards(["Ks", "Qs", "As", "Kd", "Qd", "Ah",
                      "2c", "4d", "7h", "9s", "Jc"])
    game = table(chips=(100, 200, 300), rng=rng)
    game.start_hand()
    game.command("p0", "raise_to", {"amount": 100})
    game.command("p1", "raise_to", {"amount": 200})
    game.command("p2", "check_call")
    assert game.phase == "settled"
    assert game.hand_result()["final_chips"] == {"p0": 300, "p1": 200, "p2": 100}
    assert sum(game.hand_result()["net_chips"].values()) == 0
    assert len(game.view("p0")["board"]) == 5
    # Blinds can themselves exhaust both stacks; no nonexistent turn is needed.
    short = table(instance="short", chips=(1, 1), rng=FixedCards([
        "As", "Ks", "Ah", "Kd", "2c", "4d", "7h", "9s", "Jc"]))
    short.start_hand()
    assert short.phase == "settled"
    assert short.hand_result()["final_chips"] == {"p0": 0, "p1": 2}
    before = short.view("p0")
    with pytest.raises(ValueError, match="At least two"):
        short.start_hand()
    assert short.view("p0") == before
    assert sum(short.close()["final_chips"].values()) == 2


def test_a_free_check_records_the_action_for_host_animation_and_history():
    game = table()
    game.start_hand()
    act_current(game)  # SB calls.
    act_current(game)  # BB checks; flop opens.
    before = game.view("p0")
    actor = before["seats"][before["action_on"]]["player_id"]
    assert game.view(actor)["you"]["legal"]["can_check"]
    game.command(actor, "check_call")
    after = game.view(actor)
    assert after["seats"][after["seat"]]["last_action"] == "CHECK"
    assert after["events"][-1]["action"] == "check"


@pytest.mark.parametrize("structure", ["No-Limit", "Pot-Limit", "Fixed-Limit"])
def test_supported_structures_finish_with_conserved_chips(structure):
    game = table(config=TableConfig(structure=structure, big_blind_ante=True))
    game.start_hand()
    assert sum(finish(game)["final_chips"].values()) == 2000


def test_fresh_hands_and_tables_have_distinct_result_and_event_ids():
    first = table()
    other = table(instance="visit-2")
    first.start_hand()
    other.start_hand()
    untouched = deepcopy(other.view("p0"))
    finish(first)
    old = first.hand_result()
    old_events = {e["event_id"] for e in first.view("p0")["events"]}
    first.start_hand()
    assert first.hand_result() is None
    assert not old_events & {e["event_id"] for e in first.view("p0")["events"]}
    finish(first)
    assert first.hand_result()["result_id"] != old["result_id"]
    assert other.view("p0") == untouched
    finish(other)
    assert first.close()["result_id"] != other.close()["result_id"]


@pytest.mark.parametrize("chips,config", [
    ((True, 1000), None), ((0, 1000), None), ((1000,), None),
    ((1000, 1000), TableConfig(small_blind=100, big_blind=50)),
    ((1000, 1000), TableConfig(big_blind=True)),
    ((1000, 1000), TableConfig(structure="invalid")),
])
def test_invalid_table_configuration_fails_before_play(chips, config):
    with pytest.raises(ValueError):
        table(chips=chips, config=config)
