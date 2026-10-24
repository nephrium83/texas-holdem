"""Version 1 embedding surface for poker hosts.

PokerTable owns a local rules table. The caller owns authentication, entropy,
serialization of calls, currency, persistence and presentation. This backend
trusts its host; it does not provide multiplayer fairness or a cash service.
SessionPlayer exposes the existing peer-verified Session without replacing it.
Neither adapter creates files, sockets, timers or keys on import.
"""
from __future__ import annotations

import random
from copy import deepcopy
from dataclasses import dataclass
from typing import Sequence

from . import client_view, contract, player_info
from .engine import Engine, Player

API_VERSION = 1


@dataclass(frozen=True)
class PlayerSeat:
    player_id: str
    name: str
    chips: int


@dataclass(frozen=True)
class TableConfig:
    small_blind: int = 25
    big_blind: int = 50
    structure: str = "No-Limit"
    big_blind_ante: bool = False


def _identifier(value, label):
    if (not isinstance(value, str) or not 1 <= len(value) <= 128
            or not value.isprintable() or value.strip() != value):
        raise ValueError(f"{label} must be 1–128 printable characters without edge spaces")


def _positive_chips(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


def _payload(command, payload, *, lifecycle=False):
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("Command payload must be an object")
    allowed = {"fold", "check_call", "raise_to"}
    if lifecycle:
        allowed.update({"start_game", "next_hand"})
    if not isinstance(command, str) or command not in allowed:
        raise ValueError(f"unknown command: {command!r}")
    expected = {"amount"} if command == "raise_to" else set()
    if set(payload) != expected:
        raise ValueError(f"Unexpected or missing payload fields for {command}")
    if command == "raise_to" and type(payload["amount"]) is not int:
        raise ValueError("Raise target must be an integer")
    return dict(payload)


class PokerTable:
    """An independent local table, using the shipped Engine unchanged.

    ``table_id`` is a fresh host-owned instance ID, never a reusable table
    name. Host code must serialize calls and deliver views only to the
    authenticated player they belong to. ``rng`` is supplied by that host.
    Close is allowed between hands; a live hand must finish first.
    """

    api_version = API_VERSION
    authority_mode = "trusted_host"

    def __init__(self, table_id: str, players: Sequence[PlayerSeat], *,
                 rng: random.Random, config: TableConfig | None = None):
        _identifier(table_id, "Table instance ID")
        players = tuple(players)
        if not 2 <= len(players) <= 9:
            raise ValueError("A poker table requires 2–9 players")
        if not isinstance(rng, random.Random):
            raise ValueError("The host must supply a random.Random-compatible generator")
        config = config if config is not None else TableConfig()
        if not isinstance(config, TableConfig):
            raise ValueError("config must be TableConfig")
        _positive_chips(config.small_blind, "Small blind")
        _positive_chips(config.big_blind, "Big blind")
        if config.small_blind > config.big_blind:
            raise ValueError("Small blind cannot exceed big blind")
        if config.structure not in {"No-Limit", "Pot-Limit", "Fixed-Limit"}:
            raise ValueError("Unsupported betting structure")
        if type(config.big_blind_ante) is not bool:
            raise ValueError("Big blind ante must be true or false")
        for player in players:
            if not isinstance(player, PlayerSeat):
                raise ValueError("Each player must be PlayerSeat")
            _identifier(player.player_id, "Player ID")
            _identifier(player.name, "Player name")
            _positive_chips(player.chips, "Buy-in")
        if len({p.player_id for p in players}) != len(players):
            raise ValueError("Player IDs must be unique within a table")
        self._table_id = table_id
        self._players = players
        self._seats = {p.player_id: i for i, p in enumerate(players)}
        self._initial = {p.player_id: p.chips for p in players}
        self._engine = Engine(
            [Player(i, p.name, p.chips) for i, p in enumerate(players)],
            sb=config.small_blind, bb=config.big_blind,
            structure=config.structure, rng=rng, bb_ante=config.big_blind_ante,
        )
        self._phase = "lobby"
        self._hand_initial = None
        self._hand_result = None
        self._shown = ()
        self._close_result = None

    @property
    def table_id(self):
        return self._table_id

    @property
    def phase(self):
        return self._phase

    def _chips(self):
        return {spec.player_id: p.stack
                for spec, p in zip(self._players, self._engine.players)}

    def _seat(self, player_id):
        if not isinstance(player_id, str) or player_id not in self._seats:
            raise ValueError("Player is not seated at this table")
        return self._seats[player_id]

    def start_hand(self):
        if self._phase not in {"lobby", "settled"}:
            raise ValueError("A new hand can start only at an open table between hands")
        if sum(p.stack > 0 for p in self._engine.players) < 2:
            raise ValueError("At least two players need chips to start a hand")
        self._hand_initial = self._chips()
        self._hand_result = None
        self._shown = ()
        self._engine.start_hand()
        self._phase = "betting"
        self._advance()

    def command(self, player_id, command, payload=None):
        seat = self._seat(player_id)
        payload = _payload(command, payload)
        if self._phase != "betting" or self._engine.actor != seat:
            raise ValueError("It is not this player's turn")
        legal = self._engine.legal(seat)
        if command == "raise_to":
            if (not legal["can_raise"]
                    or not legal["min_to"] <= payload["amount"] <= legal["max_to"]):
                raise ValueError("Raise target is outside the legal range")
        # Engine.act is intentionally permissive for its internal callers.
        # This public boundary rejects invalid actions before any mutation.
        contract.apply_command(self._engine, seat, command, payload)
        self._advance()
        return {"type": "command_result", "api_version": API_VERSION,
                "table_id": self.table_id, "command": command,
                "ok": True, "verdict": "applied"}

    def _advance(self):
        engine = self._engine
        while self._phase == "betting":
            if (len(engine.contested()) <= 1 or engine.betting_locked()
                    or engine.street == "showdown"):
                raw = engine.settle(force_tabled=engine.betting_locked())
                final = self._chips()
                if sum(final.values()) != sum(self._initial.values()):
                    raise RuntimeError("Poker chip conservation failed")
                self._shown = tuple(sorted(raw["shown"]))
                self._hand_result = {
                    "api_version": API_VERSION, "table_id": self.table_id,
                    "result_id": f"{self.table_id}/hand/{engine.hand_no}",
                    "hand_num": engine.hand_no,
                    "initial_chips": dict(self._hand_initial),
                    "final_chips": final,
                    "net_chips": {key: final[key] - self._hand_initial[key]
                                  for key in final},
                    "winner_ids": [self._players[i].player_id
                                   for i in sorted(raw["winners"])],
                }
                self._phase = "settled"
                return
            if engine.actor is not None:
                return
            engine.next_street()

    def view(self, player_id):
        seat = self._seat(player_id)
        snap = contract.build_snapshot(self._engine, seat)
        phase = "settled" if self._hand_result else "lobby"
        if self._phase == "betting":
            phase = "betting"
        if phase != "betting":
            snap["you"].pop("legal", None)
            snap["action_on"] = -1
        snap.update(api_version=API_VERSION, table_id=self.table_id,
                    player_id=player_id, authority_mode=self.authority_mode,
                    phase=self.phase, result=self.hand_result())
        for public, spec in zip(snap["seats"], self._players):
            public["player_id"] = spec.player_id
        winners = [] if self._hand_result is None else [
            self._seats[key] for key in self._hand_result["winner_ids"]]
        snap["turn"] = player_info.turn_view(
            self._engine, seat, phase=phase, result={"winners": winners})
        if phase != "betting":
            snap["turn"]["actor"] = -1
            snap["turn"]["actor_name"] = ""
        if self.phase == "closed":
            snap["turn"]["state"] = "table_closed"
            snap["turn"]["headline"] = "Table closed"
        snap["showdown"] = [
            {"seat": i, "player_id": self._players[i].player_id,
             "hole": [player_info.card_str(card) for card in self._engine.players[i].hole]}
            for i in self._shown
        ]
        for event in snap["events"]:
            event["event_id"] = f"{self.table_id}/hand/{self._engine.hand_no}/event/{event['seq']}"
        return deepcopy(snap)

    def hand_result(self):
        """Table-chip outcome only. External wallets must not pay this again."""
        return deepcopy(self._hand_result)

    def close(self):
        """Return final balances; repeat calls return the same cashout receipt.

        This does not transfer currency. The host applies the receipt to its
        reservation exactly once and can retry that step if its wallet fails.
        """
        if self._phase == "betting":
            raise ValueError("Finish the current hand before closing the table")
        if self._close_result is None:
            self._close_result = {
                "api_version": API_VERSION, "table_id": self.table_id,
                "result_id": f"{self.table_id}/close",
                "initial_chips": dict(self._initial), "final_chips": self._chips(),
            }
            self._phase = "closed"
        return self.result()

    def result(self):
        return deepcopy(self._close_result)


class SessionPlayer:
    """Public view/command adapter over an existing peer-verified Session.

    The host composes and authenticates Session, supplies its resources and
    owns shutdown. This retains the existing client protocol and crypto path;
    it does not turn a local PokerTable into a peer-verified table.
    """

    api_version = API_VERSION
    authority_mode = "peer_session"

    def __init__(self, session, *, start_table=None):
        self._session = session
        self._start_table = start_table

    def view(self):
        return client_view.snapshot(self._session)

    def command(self, command, payload=None):
        payload = _payload(command, payload, lifecycle=True)
        return client_view.apply_command(
            self._session, command, payload, start_table=self._start_table)


__all__ = ["API_VERSION", "PlayerSeat", "TableConfig", "PokerTable", "SessionPlayer"]
