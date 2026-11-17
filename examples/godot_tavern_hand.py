"""Run one local poker visit for the Godot tavern example."""
import json
import random

from holdem.embedding import PlayerSeat
from rpg_tavern import FakeWallet, Tavern, play_to_showdown


def main():
    wallet = FakeWallet({"ranger": 1000, "innkeeper": 1000})
    tavern = Tavern(wallet)
    players = [PlayerSeat("ranger", "Ranger", 500),
               PlayerSeat("innkeeper", "Innkeeper", 500)]
    table = tavern.sit(players, rng=random.Random(10))
    tavern.play(table)
    actions = play_to_showdown(tavern, table, [seat.player_id for seat in players])
    receipt = tavern.leave(table)
    balances = wallet.balances()
    if tavern.leave(table) != receipt or wallet.balances() != balances:
        raise RuntimeError("Repeated settlement changed the receipt or wallet")
    if tavern.phase(table) != "closed" or wallet.reserved_total() or sum(balances.values()) != 2000:
        raise RuntimeError("The visit did not close with all chips accounted for")
    print(json.dumps({"actions": actions, "receipt": receipt, "balances": balances,
                      "phase": tavern.phase(table)}))


if __name__ == "__main__":
    main()
