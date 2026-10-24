"""A second host for the public poker API: a local RPG tavern.

Run ``python rpg_tavern.py --demo`` after installing the holdem wheel. This
file deliberately uses only the public embedding facade and Python's standard
library; it can be copied outside the poker checkout without modification.

The wallet is an in-memory example, not a durable or real-money ledger. Only
the trusted host may create tables, bind authenticated players to commands,
and submit the table's close result for payout.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import random
from typing import Mapping, Sequence
from uuid import uuid4

from holdem.embedding import API_VERSION, PlayerSeat, PokerTable, TableConfig


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _chips(value: object, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError("chips must be a positive integer" if positive
                         else "chips must be a nonnegative integer")
    return value


def _amounts(values: object, *, positive: bool = False) -> dict[str, int]:
    if not isinstance(values, dict) or not values:
        raise ValueError("player amounts must be a nonempty object")
    return {_identity(player_id, "player_id"): _chips(chips, positive=positive)
            for player_id, chips in values.items()}


class FakeWallet:
    """Host-owned process-local balances and atomic buy-in/payout examples.

    Repeated identical reservations or payouts are harmless. Reusing a table
    ID with different data is an error. There is no I/O, crash recovery,
    concurrent transaction handling, or authority over untrusted results.
    """

    def __init__(self, balances: Mapping[str, int]):
        self._balances = _amounts(dict(balances))
        self._reservations: dict[str, dict[str, int]] = {}
        self._settlements: dict[str, dict] = {}

    def balances(self) -> dict[str, int]:
        return dict(self._balances)

    def reserved_total(self) -> int:
        return sum(sum(amounts.values()) for table_id, amounts
                   in self._reservations.items()
                   if table_id not in self._settlements)

    def reserve(self, table_id: str, buy_ins: Mapping[str, int]) -> bool:
        """Reserve every buy-in together; return whether anything changed."""
        table_id = _identity(table_id, "table_id")
        amounts = _amounts(dict(buy_ins), positive=True)
        if table_id in self._settlements:
            raise ValueError("a closed table ID cannot be reused")
        previous = self._reservations.get(table_id)
        if previous is not None:
            if previous != amounts:
                raise ValueError("table already has a different reservation")
            return False
        updated = dict(self._balances)
        for player_id, amount in amounts.items():
            if player_id not in updated:
                raise ValueError(f"unknown wallet player: {player_id}")
            if updated[player_id] < amount:
                raise ValueError(f"insufficient funds for {player_id}")
            updated[player_id] -= amount
        self._balances = updated
        self._reservations[table_id] = dict(amounts)
        return True

    def settle(self, result: dict) -> bool:
        """Credit one trusted close result once, after all checks succeed."""
        if not isinstance(result, dict):
            raise ValueError("close result must be an object")
        if type(result.get("api_version")) is not int or result["api_version"] != API_VERSION:
            raise ValueError("unsupported close result API version")
        table_id = _identity(result.get("table_id"), "table_id")
        if table_id not in self._reservations:
            raise ValueError("close result has no matching reservation")
        if result.get("result_id") != f"{table_id}/close":
            raise ValueError("close result ID does not match the table")
        initial = _amounts(result.get("initial_chips"))
        final = _amounts(result.get("final_chips"))
        if initial != self._reservations[table_id]:
            raise ValueError("initial chips do not match the reserved buy-ins")
        if set(final) != set(initial):
            raise ValueError("payout players do not match the reservation")
        if sum(initial.values()) != sum(final.values()):
            raise ValueError("payout does not conserve the reserved chips")
        previous = self._settlements.get(table_id)
        if previous is not None:
            if result != previous:
                raise ValueError("table already has a different close result")
            return False
        # Construct the entire next balance state before publishing any change.
        saved_result = deepcopy(result)
        updated = dict(self._balances)
        for player_id, chips in final.items():
            updated[player_id] += chips
        self._balances = updated
        self._settlements[table_id] = saved_result
        return True


class Tavern:
    """World interaction adapter: sit, play, inspect, act, then leave."""

    def __init__(self, wallet: FakeWallet):
        self.wallet = wallet
        self._tables: dict[str, PokerTable] = {}

    def sit(self, players: Sequence[PlayerSeat], *,
            rng: random.Random | None = None,
            config: TableConfig | None = None) -> str:
        # A table ID identifies one reservation lifetime, not a reusable chair
        # or tavern location. A fresh visit must get a fresh ID.
        table_id = f"tavern-{uuid4()}"
        seats = tuple(players)
        table = PokerTable(table_id, seats, rng=rng if rng is not None
                           else random.SystemRandom(), config=config)
        self.wallet.reserve(table_id, {seat.player_id: seat.chips for seat in seats})
        self._tables[table_id] = table
        return table_id

    def play(self, table_id: str) -> None:
        self._tables[table_id].start_hand()

    def phase(self, table_id: str) -> str:
        return self._tables[table_id].phase

    def view(self, table_id: str, player_id: str) -> dict:
        return self._tables[table_id].view(player_id)

    def command(self, table_id: str, player_id: str, command: str,
                payload: dict | None = None) -> dict:
        # A real host obtains player_id from its authenticated connection, not
        # a client-provided field. This local demonstration has no connections.
        return self._tables[table_id].command(player_id, command, payload)

    def leave(self, table_id: str) -> dict:
        # close() rejects an unfinished hand. The host must decide a separate
        # disconnect/abandonment policy before offering that in a network game.
        result = self._tables[table_id].close()
        self.wallet.settle(result)
        return result


def _assert_private(snapshot: dict) -> None:
    if snapshot["phase"] == "betting":
        if len(snapshot["you"].get("hole", [])) != 2:
            raise AssertionError("the viewer did not receive their own cards")
        if any("hole" in seat for seat in snapshot["seats"]):
            raise AssertionError("a public seat exposed private hole cards")


def play_to_showdown(tavern: Tavern, table_id: str,
                     player_ids: Sequence[str]) -> int:
    """Exercise the public player interface, calling/checking every turn."""
    actions = 0
    while tavern.phase(table_id) == "betting":
        snapshots = [tavern.view(table_id, player_id) for player_id in player_ids]
        for snapshot in snapshots:
            _assert_private(snapshot)
        actors = [snapshot for snapshot in snapshots if snapshot["you"].get("legal")]
        if len(actors) != 1:
            raise AssertionError("a betting table must offer exactly one player a turn")
        actor = actors[0]
        result = tavern.command(table_id, actor["player_id"], "check_call")
        if result.get("ok") is False:
            raise AssertionError(f"a legal call/check was rejected: {result}")
        actions += 1
        if actions > 100:
            raise AssertionError("the hand did not complete")
    if tavern.phase(table_id) != "settled":
        raise AssertionError("the hand did not settle")
    return actions


def run_demo() -> dict:
    """Run two independent tavern tables and a fresh return visit."""
    wallet = FakeWallet({"ranger": 2000, "mage": 2000,
                         "bard": 2000, "innkeeper": 2000})
    tavern = Tavern(wallet)
    parties = [
        (PlayerSeat("ranger", "Ranger", 500), PlayerSeat("mage", "Mage", 500)),
        (PlayerSeat("bard", "Bard", 500), PlayerSeat("innkeeper", "Innkeeper", 500)),
    ]
    table_ids = [tavern.sit(party, rng=random.Random(index + 10))
                 for index, party in enumerate(parties)]
    for table_id in table_ids:
        tavern.play(table_id)
    untouched = tavern.view(table_ids[1], "bard")
    actions = play_to_showdown(tavern, table_ids[0], ["ranger", "mage"])
    if tavern.view(table_ids[1], "bard") != untouched:
        raise AssertionError("playing one table changed another table")
    actions += play_to_showdown(tavern, table_ids[1], ["bard", "innkeeper"])
    first_result = tavern.leave(table_ids[0])
    paid_balances = wallet.balances()
    if tavern.leave(table_ids[0]) != first_result or wallet.balances() != paid_balances:
        raise AssertionError("a repeated leave paid twice")
    tavern.leave(table_ids[1])
    fresh = tavern.sit(parties[0], rng=random.Random(12))
    if fresh in table_ids or tavern.phase(fresh) != "lobby":
        raise AssertionError("a return visit reused the old table")
    tavern.leave(fresh)  # Leaving before playing returns the entire buy-in.
    total = sum(wallet.balances().values()) + wallet.reserved_total()
    if total != 8000 or wallet.reserved_total() != 0:
        raise AssertionError("tavern transfers did not conserve funds")
    return {"demo": "RPG tavern", "api_version": API_VERSION,
            "tables_completed": 2, "fresh_visit": True,
            "private_views_checked": True, "duplicate_payout_prevented": True,
            "actions": actions, "wallet_balances": wallet.balances(),
            "total_funds": total}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true",
                        help="play two local tavern tables and check host integration")
    args = parser.parse_args()
    if not args.demo:
        parser.error("choose --demo to run the local tavern example")
    print(json.dumps(run_demo(), indent=2))


if __name__ == "__main__":
    main()
