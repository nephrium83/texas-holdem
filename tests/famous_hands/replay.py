"""Strict, test-only replay of public Hold'em hand histories.

This drives the production ReplicaTable, but never the bot, Godot, network,
or cryptographic dealing. Cards are supplied as recovered public evidence.
Unlike Engine.act(), the harness rejects rather than clamps a source raise.
"""
from __future__ import annotations

from typing import Any

from holdem.engine import Card, evaluate
from holdem.p2p.replica_table import (
    PHASE_BETTING,
    PHASE_HAND_OVER,
    PHASE_SHOWDOWN,
    PHASE_STREET_OVER,
    ReplicaTable,
)


_STREETS = ("preflop", "flop", "turn", "river")
_BOARD_COUNTS = {"preflop": 0, "flop": 3, "turn": 1, "river": 1}


class AnteRuleMismatch(AssertionError):
    """A demonstrated disagreement between saved house rules and WSOP antes."""

    def __init__(self, message: str, evidence: dict):
        super().__init__(message)
        self.evidence = evidence


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _integer(value: Any, label: str) -> int:
    _check(type(value) is int, f"{label}: expected an integer, got {value!r}")
    return value


def _card(label: str) -> Card:
    _check(isinstance(label, str), f"invalid card {label!r}")
    token = label.strip()
    if len(token) == 3 and token[:2] == "10":
        token = "T" + token[2:]
    _check(len(token) == 2 and token[0].upper() in "23456789TJQKA"
           and token[1].lower() in "cdhs", f"invalid card {label!r}")
    return Card("23456789TJQKA".index(token[0].upper()) + 2,
                "cdhs".index(token[1].lower()))


def _label(card: Card) -> str:
    return "23456789TJQKA"[card.v - 2] + "cdhs"[card.s]


def _snapshot(replica: ReplicaTable, kind: str, **details: Any) -> dict:
    engine = replica.engine
    return {
        "kind": kind,
        "street": engine.street,
        "phase": replica.phase,
        "actor": replica.actor,
        "board": [_label(card) for card in engine.board],
        "pot": engine.pot,
        "current_bet": engine.current_bet,
        "stacks": replica.stacks,
        "bets": [player.bet for player in engine.players],
        "folded": [player.folded for player in engine.players],
        "all_in": [player.all_in for player in engine.players],
        **details,
    }


def _synced(replicas: list[ReplicaTable], context: str, bank: int) -> None:
    _check(len({replica.state_digest() for replica in replicas}) == 1,
           f"{context}: betting replicas diverged")
    for replica in replicas:
        _check(all(player.stack >= 0 for player in replica.engine.players),
               f"{context}: negative stack")
        _check(sum(replica.stacks) + replica.engine.pot == bank,
               f"{context}: chips were created or lost")


def _expected_settlement(replica: ReplicaTable) -> tuple[list | None, list[dict]]:
    """Calculate payouts from pre-settle commitments, not engine results.

    Pot boundaries are contribution thresholds. A contribution above the
    runner-up live stake is returned first; folded stakes stay in the pot.
    Scores use the production evaluator, while fixture winners and optional
    fixture stacks provide the independent historical outcome assertions.
    """
    engine = replica.engine
    players = engine.players
    live = [player.total_live for player in players]
    refund = None
    positive = sorted((value for value in live if value > 0), reverse=True)
    if len(positive) >= 2 and positive[0] > positive[1]:
        seat = live.index(positive[0])
        amount = positive[0] - positive[1]
        refund = [seat, amount]
        live[seat] -= amount

    dead = sum(player.total_dead for player in players)
    alive = [player.idx for player in engine.contested()]
    scores = ({seat: evaluate(players[seat].hole + engine.board) for seat in alive}
              if len(alive) > 1 else {})
    layers: list[dict] = []
    previous = 0
    for boundary in sorted(set(live) - {0}):
        contributing = [seat for seat, total in enumerate(live)
                        if total >= boundary]
        amount = (boundary - previous) * len(contributing)
        eligible = [seat for seat in alive if live[seat] >= boundary]
        previous = boundary
        _check(bool(eligible), "no eligible player for live contributions")
        if layers and layers[-1]["eligible"] == eligible:
            layers[-1]["amount"] += amount
        else:
            layers.append({"amount": amount, "eligible": eligible})

    # Antes are dead money in the main pot, not additional live stakes
    # that can create a private side pot for the ante's posting seat.
    _check(bool(layers), "no main pot for dead contributions")
    layers[0]["amount"] += dead

    for pot in layers:
        if len(alive) == 1:
            winners = list(alive)
        else:
            best = max(scores[seat] for seat in pot["eligible"])
            winners = [seat for seat in pot["eligible"] if scores[seat] == best]
        share, remainder = divmod(pot["amount"], len(winners))
        payouts = {seat: share for seat in winners}
        for offset in range(1, len(players) + 1):
            seat = (engine.button + offset) % len(players)
            if remainder and seat in payouts:
                payouts[seat] += 1
                remainder -= 1
        _check(remainder == 0, "odd-chip allocation failed")
        pot.update(winners=winners, payouts=payouts)
    _check(sum(pot["amount"] for pot in layers) == sum(live) + dead,
           "independent pot layers do not account for every chip")
    return refund, layers


def replay_case(case: dict) -> dict:
    """Replay a fixture, raising AssertionError on any source/state mismatch.

    Board arrays contain newly dealt cards (3/1/1). A call amount is the
    incremental amount paid; a raise amount is the absolute street target.
    Unknown folded holes may be omitted. Every contested hole is required.
    """
    hand_id = str(case["id"])
    names = list(case["names"])
    stacks = list(case["stacks"])
    _check(2 <= len(names) <= 9 and len(names) == len(stacks),
           f"{hand_id}: need 2-9 aligned names and stacks")
    for seat, stack in enumerate(stacks):
        _check(_integer(stack, f"stack {seat}") > 0,
               f"{hand_id}: every replay seat must start with chips")
    sb = _integer(case["sb"], "small blind")
    bb = _integer(case["bb"], "big blind")
    button = _integer(case["button"], "button")
    _check(0 < sb < bb, f"{hand_id}: invalid blinds")
    _check(0 <= button < len(names), f"{hand_id}: invalid button")
    _check(type(case.get("bb_ante", False)) is bool,
           f"{hand_id}: bb_ante must be boolean")

    known: set[tuple[int, int]] = set()

    def cards(labels: list[str], context: str) -> list[Card]:
        _check(isinstance(labels, list), f"{context}: expected a card list")
        result = [_card(label) for label in labels]
        for card in result:
            key = (card.v, card.s)
            _check(key not in known, f"{hand_id}: duplicate known card {_label(card)}")
            known.add(key)
        return result

    holes: dict[int, list[Card]] = {}
    for raw_seat, labels in case.get("holes", {}).items():
        seat = int(raw_seat)
        _check(0 <= seat < len(names) and seat not in holes,
               f"{hand_id}: invalid/repeated hole-card seat {raw_seat!r}")
        _check(len(labels) == 2, f"{hand_id}: seat {seat} needs two hole cards")
        holes[seat] = cards(labels, f"seat {seat} holes")

    street_fixtures = list(case["streets"])
    _check(1 <= len(street_fixtures) <= 4, f"{hand_id}: missing/extra streets")
    prepared_streets = []
    for index, street in enumerate(street_fixtures):
        name = street["name"]
        _check(name == _STREETS[index], f"{hand_id}: streets must start preflop and progress in order")
        board = cards(street.get("board", []), f"{name} board")
        _check(len(board) == _BOARD_COUNTS[name], f"{hand_id}: wrong {name} card count")
        prepared_streets.append((name, board, street["actions"]))

    bank = sum(stacks)
    replicas = [ReplicaTable(session_id=f"historical:{hand_id}", hand_no=1,
                             names=names, stacks=stacks, sb=sb, bb=bb)
                for _ in range(2)]
    for replica in replicas:
        # This setting exists on the engine but not the public ReplicaTable
        # constructor. It does not add an ante option to the shipped UI.
        replica.engine.bb_ante = case.get("bb_ante", False)
        _check(replica.start_hand((button - 1) % len(names)),
               f"{hand_id}: hand did not start")
        _check(replica.button == button, f"{hand_id}: incorrect button")
        replica.set_all_holes(holes)
    _synced(replicas, f"{hand_id} start", bank)
    trace = [_snapshot(replicas[0], "start", button=button)]
    sequence = 0

    for name, board, actions in prepared_streets:
        if name != "preflop":
            for replica in replicas:
                _check(replica.phase == PHASE_STREET_OVER,
                       f"{hand_id}: cannot deal {name} before betting closes")
                old_board = list(replica.engine.board)
                replica.advance_street(board)
                _check([_label(card) for card in replica.engine.board]
                       == [_label(card) for card in old_board + board],
                       f"{hand_id}: wrong injected board on {name}")
            _synced(replicas, f"{hand_id} {name}", bank)
            trace.append(_snapshot(replicas[0], "street"))

        for raw_action in actions:
            _check(len(raw_action) == 3, f"{hand_id}: malformed action {raw_action!r}")
            seat, action, amount = raw_action
            seat = _integer(seat, "acting seat")
            amount = _integer(amount, "action amount")
            context = f"{hand_id} {name} action {sequence} ({names[seat] if 0 <= seat < len(names) else seat})"
            _check(0 <= seat < len(names), f"{context}: invalid seat")
            _check(action in ("check", "call", "fold", "raise"),
                   f"{context}: unsupported action {action!r}")
            for replica in replicas:
                engine = replica.engine
                _check(engine.street == name, f"{context}: incorrect street")
                _check(replica.phase == PHASE_BETTING and replica.actor == seat,
                       f"{context}: out-of-turn action (actor {replica.actor})")
                legal = engine.legal(seat)
                if action == "raise":
                    _check(legal["can_raise"] and legal["min_to"] <= amount <= legal["max_to"],
                           f"{context}: illegal exact raise-to {amount}; legal {legal}")
                    increment = amount - engine.players[seat].bet
                elif action == "call":
                    _check(legal["to_call"] > 0 and amount == legal["to_call"],
                           f"{context}: call must pay exactly {legal['to_call']}, got {amount}")
                    increment = amount
                else:
                    _check(amount == 0, f"{context}: {action} amount must be zero")
                    if action == "check":
                        _check(legal["can_check"], f"{context}: check faces a bet")
                    increment = 0
                before_stacks = replica.stacks
                before_pot = engine.pot
                before_bet = engine.players[seat].bet
                mapped = "call" if action == "check" else action
                _check(replica.apply_action(sequence, seat, mapped, amount) == "applied",
                       f"{context}: action rejected")
                expected_stacks = list(before_stacks)
                expected_stacks[seat] -= increment
                _check(replica.stacks == expected_stacks,
                       f"{context}: incorrect chip debit")
                _check(engine.pot == before_pot + increment,
                       f"{context}: incorrect pot increment")
                _check(engine.players[seat].bet == before_bet + increment,
                       f"{context}: engine changed the source bet size")
                if action == "fold":
                    _check(engine.players[seat].folded, f"{context}: fold was not applied")
            _synced(replicas, context, bank)
            trace.append(_snapshot(replicas[0], "action", sequence=sequence,
                                   seat=seat, action=action, amount=amount))
            sequence += 1
        _check(replicas[0].phase != PHASE_BETTING,
               f"{hand_id}: source actions incomplete on {name}; actor {replicas[0].actor} remains")

    replica = replicas[0]
    _check(replica.phase in (PHASE_HAND_OVER, PHASE_SHOWDOWN),
           f"{hand_id}: source ends before the hand is ready to settle")
    if replica.phase == PHASE_SHOWDOWN:
        _check(len(replica.engine.board) == 5,
               f"{hand_id}: cannot invent unknown showdown board cards")
        missing = [player.idx for player in replica.engine.contested()
                   if player.idx not in holes]
        _check(not missing, f"{hand_id}: missing contested hole cards for seats {missing}")
    refund, independent_pots = _expected_settlement(replica)
    before_stacks = replica.stacks
    before_live = [player.total_live for player in replica.engine.players]
    results = [replica.finish() for replica in replicas]
    _check(results[0] == results[1], f"{hand_id}: normalized settlement results differ")
    _synced(replicas, f"{hand_id} settled", bank)
    result = results[0]
    expected = case["expected"]
    independent_winners = sorted({seat for pot in independent_pots for seat in pot["winners"]})
    _check(sorted(expected["winners"]) == independent_winners,
           f"{hand_id}: fixture winner contradicts the independent oracle")
    _check(expected["pots"] == [pot["amount"] for pot in independent_pots],
           f"{hand_id}: fixture pots contradict the independent oracle")
    _check(expected["refund"] == refund,
           f"{hand_id}: fixture refund contradicts the independent oracle")
    predicted_stacks = [before_stacks[seat] + sum(pot["payouts"].get(seat, 0)
                       for pot in independent_pots) for seat in range(len(names))]
    if refund is not None:
        predicted_stacks[refund[0]] += refund[1]
    if "stacks" in expected:
        _check(expected["stacks"] == predicted_stacks,
               f"{hand_id}: fixture stacks contradict the independent oracle")
    _check(result["refund"] == refund, f"{hand_id}: incorrect uncalled-bet refund")
    known_legacy_pots = [
        dict(amount=amount, eligible=eligible, winners=[1],
             runs=[dict(amount=amount, payouts={1: amount}, winners=[1])])
        for amount, eligible in ((343000000, [0, 1]), (2500000, [1]))
    ]
    if (case.get("bb_ante") and len(names) == 2
            and before_live == [171500000, 171500000]
            and result["pots"] == known_legacy_pots
            and result["winners"] == [1] and replica.stacks == [0, 671500000]):
        raise AnteRuleMismatch(
            f"{hand_id}: saved house rules create a private ante pot; WSOP requires one shared main pot",
            dict(actual_pots=result["pots"], expected_pots=independent_pots,
                 stacks=replica.stacks, winners=result["winners"], action_count=sequence,
                 trace=trace + [_snapshot(replica, "settlement", refund=refund)]))
    _check(len(result["pots"]) == len(independent_pots), f"{hand_id}: incorrect pot count")
    payouts = [0] * len(names)
    for index, (actual, predicted) in enumerate(zip(result["pots"], independent_pots)):
        _check(actual["amount"] == predicted["amount"]
               and actual["eligible"] == predicted["eligible"],
               f"{hand_id}: incorrect side-pot layer {index}")
        _check(actual["winners"] == predicted["winners"],
               f"{hand_id}: incorrect winner of pot {index}")
        _check(len(actual["runs"]) == 1, f"{hand_id}: unexpected multiple runouts")
        run = actual["runs"][0]
        _check(run["amount"] == predicted["amount"]
               and run["winners"] == predicted["winners"]
               and run["payouts"] == predicted["payouts"],
               f"{hand_id}: incorrect split or odd-chip payout in pot {index}")
        _check(sum(run["payouts"].values()) == actual["amount"],
               f"{hand_id}: payouts do not sum to pot {index}")
        for seat, amount in run["payouts"].items():
            payouts[seat] += amount
    expected_stacks = [stack + payout for stack, payout in zip(before_stacks, payouts)]
    if refund is not None:
        expected_stacks[refund[0]] += refund[1]
    _check(replica.stacks == expected_stacks, f"{hand_id}: stacks disagree with payouts/refund")
    _check(result["winners"] == sorted({seat for pot in independent_pots for seat in pot["winners"]}),
           f"{hand_id}: global winners disagree with pot winners")

    amounts = [pot["amount"] for pot in result["pots"]]
    _check(result["winners"] == sorted(expected["winners"]),
           f"{hand_id}: winner differs from sourced outcome")
    _check(amounts == expected["pots"],
           f"{hand_id}: pot amounts {amounts} differ from sourced {expected['pots']}")
    _check(result["refund"] == expected["refund"],
           f"{hand_id}: refund differs from fixture")
    if "stacks" in expected:
        _check(replica.stacks == expected["stacks"], f"{hand_id}: final stacks differ from fixture")
    if "pot_winners" in expected:
        _check([pot["winners"] for pot in result["pots"]] == expected["pot_winners"],
               f"{hand_id}: individual pot winners differ from fixture")
    if "payouts" in expected:
        _check(payouts == expected["payouts"], f"{hand_id}: aggregate winnings differ from fixture")
    trace.append(_snapshot(replica, "settlement", payouts=payouts, refund=refund))
    notes = list(case.get("notes", []))
    if case.get("bb_ante", False):
        notes.append("Big-blind ante is configured through the low-level Engine setting for this test; it is not a public ReplicaTable constructor option.")
    return {
        "id": hand_id,
        "title": case.get("title", hand_id),
        "names": names,
        "initial_stacks": stacks,
        "holes": case.get("holes", {}),
        "passed": True,
        "action_count": sequence,
        "winners": result["winners"],
        "pots": amounts,
        "refund": result["refund"],
        "stacks": replica.stacks,
        "payouts": payouts,
        "pot_details": result["pots"],
        "trace": trace,
        "coverage": case.get("coverage", []),
        "notes": notes,
        "sources": case.get("sources", []),
    }


replay_hand = replay_case
