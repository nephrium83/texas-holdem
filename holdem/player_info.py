"""Authoritative, display-ready player information.

The rendering clients should not infer poker meaning from raw state. This
module turns engine/session data into stable turn, hand, event, verification,
and settlement views that Tkinter and Godot can render directly.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .engine import Card, best_five, evaluate, hand_name


_RANK_NAME = {
    2: "Two",
    3: "Three",
    4: "Four",
    5: "Five",
    6: "Six",
    7: "Seven",
    8: "Eight",
    9: "Nine",
    10: "Ten",
    11: "Jack",
    12: "Queen",
    13: "King",
    14: "Ace",
}

_RANK_PLURAL = {
    2: "Twos",
    3: "Threes",
    4: "Fours",
    5: "Fives",
    6: "Sixes",
    7: "Sevens",
    8: "Eights",
    9: "Nines",
    10: "Tens",
    11: "Jacks",
    12: "Queens",
    13: "Kings",
    14: "Aces",
}


def card_str(card: Card | Sequence[int]) -> str:
    """Return the wire-format card string for a Card or normalized pair."""
    if isinstance(card, Card):
        value, suit = card.v, card.s
    else:
        value, suit = int(card[0]), int(card[1])
    rank = str(value) if value <= 10 else {11: "J", 12: "Q", 13: "K", 14: "A"}[value]
    return f"{rank}{'cdhs'[suit]}"


def _score_tuple(score: Sequence[Any]) -> tuple[int, tuple[int, ...]]:
    return int(score[0]), tuple(int(v) for v in score[1])


def describe_score(score: Sequence[Any]) -> str:
    """Human-readable hand strength including the deciding ranks."""
    category, tie = _score_tuple(score)
    if category == 8:
        if tie[0] == 14:
            return "Royal Flush"
        return f"{_RANK_NAME[tie[0]]}-high Straight Flush"
    if category == 7:
        return f"Four {_RANK_PLURAL[tie[0]]}, {_RANK_NAME[tie[1]]} kicker"
    if category == 6:
        return f"{_RANK_PLURAL[tie[0]]} full of {_RANK_PLURAL[tie[1]]}"
    if category == 5:
        return f"{_RANK_NAME[tie[0]]}-high Flush"
    if category == 4:
        return f"{_RANK_NAME[tie[0]]}-high Straight"
    if category == 3:
        return (
            f"Three {_RANK_PLURAL[tie[0]]}, "
            f"{_RANK_NAME[tie[1]]}-{_RANK_NAME[tie[2]]} kickers"
        )
    if category == 2:
        return (
            f"{_RANK_PLURAL[tie[0]]} and {_RANK_PLURAL[tie[1]]}, "
            f"{_RANK_NAME[tie[2]]} kicker"
        )
    if category == 1:
        kickers = "-".join(_RANK_NAME[value] for value in tie[1:])
        return f"Pair of {_RANK_PLURAL[tie[0]]}, {kickers} kickers"
    kickers = "-".join(_RANK_NAME[value] for value in tie[1:])
    suffix = f", {kickers} kickers" if kickers else ""
    return f"{_RANK_NAME[tie[0]]}-high{suffix}"


def made_hand_view(hole: Sequence[Card], board: Sequence[Card]) -> dict | None:
    """Describe the local player's made hand once five cards are available."""
    if len(hole) != 2 or len(board) < 3:
        return None
    cards = list(hole) + list(board)
    score = evaluate(cards)
    best = best_five(cards)
    board_plays = len(board) == 5 and evaluate(board) == score
    return {
        "name": hand_name(score),
        "description": describe_score(score),
        "best_five": [card_str(card) for card in best],
        "board_plays": board_plays,
    }


def _result_winners(result: Mapping[str, Any] | None) -> set[int]:
    if not result:
        return set()
    return {int(seat) for seat in result.get("winners", [])}


def turn_view(
    engine,
    seat: int,
    *,
    phase: str = "betting",
    result: Mapping[str, Any] | None = None,
    session_over: bool = False,
    session_winner: int | None = None,
    eliminated: bool = False,
    void_reason: str | None = None,
) -> dict:
    """Build the complete, current turn context for one local seat."""
    player = engine.players[seat]
    actor = engine.actor if engine.actor is not None else -1
    actor_name = engine.players[actor].name if 0 <= actor < len(engine.players) else ""
    street = engine.street
    street_label = street.replace("_", " ").title()
    winners = _result_winners(result)

    if session_over:
        state = "match_complete"
        if session_winner == seat:
            headline = "You won the match"
        elif session_winner is not None and 0 <= session_winner < len(engine.players):
            headline = f"{engine.players[session_winner].name} won the match"
        else:
            headline = "Match complete"
    elif eliminated:
        state = "eliminated"
        headline = "You are out | table still playing"
    elif phase == "void":
        state = "voided"
        headline = "Hand voided | chips restored"
    elif phase == "settled":
        state = "hand_complete"
        cashout = result.get("cashout") if result else None
        if cashout and int(cashout.get("seat", -1)) == seat:
            headline = f"You cashed out for {int(cashout['paid'])}"
        elif seat in winners:
            headline = f"You won {player.won}"
        elif player.folded:
            headline = "You folded | hand complete"
        else:
            headline = "Hand complete"
    elif phase == "lobby":
        state = "lobby"
        headline = "Waiting for players"
    elif phase == "dealing":
        state = "dealing"
        headline = "Dealing | verifying peer contributions"
    elif player.folded:
        state = "folded_waiting"
        headline = "You folded | hand in progress"
    elif player.all_in:
        state = "all_in_waiting"
        headline = "All-in | waiting for the runout"
    elif actor == seat:
        state = "your_turn"
        headline = f"Your turn | {street_label}"
    elif actor >= 0:
        state = "waiting"
        headline = f"Waiting for {actor_name}"
    else:
        state = "resolving"
        headline = f"Resolving {street_label}"

    opponents = [
        other for other in engine.contested()
        if other.idx != seat
    ]
    opponent_total = max(
        (other.stack + other.bet for other in opponents),
        default=player.stack + player.bet,
    )
    effective_stack = min(player.stack + player.bet, opponent_total)

    view = {
        "state": state,
        "headline": headline,
        "street": street,
        "street_label": street_label,
        "actor": actor,
        "actor_name": actor_name,
        "pot": engine.pot,
        "your_stack": player.stack,
        "your_bet": player.bet,
        "effective_stack": effective_stack,
        "void_reason": void_reason if phase == "void" else None,
    }

    if phase == "betting" and actor == seat:
        legal = engine.legal(seat)
        to_call = int(legal["to_call"])
        pot_after_call = int(legal["pot"]) + to_call
        pot_odds = (to_call / pot_after_call) if to_call and pot_after_call else 0.0
        view["headline"] += (
            " | check available" if legal["can_check"]
            else f" | {to_call} to call"
        )
        view["decision"] = {
            "action_label": "Check" if legal["can_check"] else f"Call {to_call}",
            "to_call": to_call,
            "can_check": bool(legal["can_check"]),
            "can_raise": bool(legal["can_raise"]),
            "pot_now": int(legal["pot"]),
            "pot_after_call": pot_after_call,
            "pot_odds": pot_odds,
            "pot_odds_pct": round(pot_odds * 100, 1),
            "stack_after_call": player.stack - to_call,
            "effective_stack": effective_stack,
            "min_raise_to": int(legal["min_to"]),
            "max_raise_to": int(legal["max_to"]),
            "call_is_all_in": to_call > 0 and to_call >= player.stack,
        }
    return view


def verification_view(phase: str, void_reason: str | None = None) -> dict:
    """Describe what the client may truthfully claim about deal verification."""
    if phase == "lobby":
        return {"state": "not_started", "label": "Verification starts with the deal"}
    if phase == "dealing":
        return {"state": "in_progress", "label": "Verifying peer deal contributions"}
    if phase == "betting":
        return {"state": "audit_pending", "label": "Deal active | final audit pending"}
    if phase == "settled":
        return {"state": "verified", "label": "Deal and settlement verified"}
    return {
        "state": "voided",
        "label": f"Hand voided | {void_reason or 'verification failed'}",
    }


def _normalized(value: Any) -> Any:
    if isinstance(value, Card):
        return card_str(value)
    if isinstance(value, dict):
        return {str(key): _normalized(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_normalized(item) for item in value]
    return value


def event_views(events: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Return JSON-safe, append-only current-hand event records."""
    return [_normalized(dict(event)) for event in events]


def _mapping_value(mapping: Mapping[Any, Any], key: int, default=None):
    if key in mapping:
        return mapping[key]
    return mapping.get(str(key), default)


def _cards_view(cards: Sequence[Any]) -> list[str]:
    return [card_str(card) for card in cards]


def settlement_view(
    engine,
    result: Mapping[str, Any],
    seat: int,
    *,
    starting_stacks: Sequence[int] | None = None,
) -> dict:
    """Build a display-ready, pot-by-pot settlement summary."""
    winners = _result_winners(result)
    player = engine.players[seat]
    starting_stack = (
        int(starting_stacks[seat])
        if starting_stacks is not None and seat < len(starting_stacks)
        else None
    )
    net = player.stack - starting_stack if starting_stack is not None else None

    pots = []
    runs = result.get("runs", [])
    for pot_index, pot in enumerate(result.get("pots", [])):
        awards = []
        for run_index, award in enumerate(pot.get("runs", [])):
            award_winners = [int(winner) for winner in award.get("winners", [])]
            payout_map = award.get("payouts", {})
            payouts = [
                {
                    "seat": winner,
                    "name": engine.players[winner].name,
                    "amount": int(_mapping_value(payout_map, winner, 0)),
                }
                for winner in award_winners
            ]
            hand = None
            if run_index < len(runs) and award_winners:
                score_map = runs[run_index].get("scores", {})
                score = _mapping_value(score_map, award_winners[0])
                if score is not None:
                    hand = {
                        "name": hand_name(_score_tuple(score)),
                        "description": describe_score(score),
                    }
            awards.append({
                "run": run_index + 1,
                "amount": int(award.get("amount", 0)),
                "winners": award_winners,
                "payouts": payouts,
                "hand": hand,
            })
        pots.append({
            "index": pot_index,
            "label": "Main pot" if pot_index == 0 else f"Side pot {pot_index}",
            "amount": int(pot.get("amount", 0)),
            "eligible": [int(value) for value in pot.get("eligible", [])],
            "awards": awards,
        })

    showdown = []
    order = [int(value) for value in result.get("order", [])]
    shown = {int(value) for value in result.get("shown", [])}
    mucked = {int(value) for value in result.get("mucked", [])}
    for player_seat in order:
        hands = []
        for run_index, run in enumerate(runs):
            score = _mapping_value(run.get("scores", {}), player_seat)
            best = _mapping_value(run.get("best", {}), player_seat, [])
            if score is None:
                continue
            hands.append({
                "run": run_index + 1,
                "name": hand_name(_score_tuple(score)),
                "description": describe_score(score),
                "best_five": _cards_view(best),
            })
        showdown.append({
            "seat": player_seat,
            "name": engine.players[player_seat].name,
            "shown": player_seat in shown,
            "mucked": player_seat in mucked,
            "won": int(engine.players[player_seat].won),
            "hands": hands,
        })

    refund = None
    raw_refund = result.get("refund")
    if raw_refund:
        refund_seat, amount = int(raw_refund[0]), int(raw_refund[1])
        refund = {
            "seat": refund_seat,
            "name": engine.players[refund_seat].name,
            "amount": amount,
        }

    cashout = result.get("cashout")
    if cashout and int(cashout.get("seat", -1)) == seat:
        outcome = "cashout"
        headline = f"You cashed out for {int(cashout['paid'])}"
    elif seat in winners:
        outcome = "won"
        headline = f"You won {player.won}"
    elif player.folded:
        outcome = "folded"
        headline = "You folded | hand complete"
    else:
        outcome = "lost"
        headline = "Hand complete"

    return {
        "headline": headline,
        "outcome": outcome,
        "total_pot": sum(pot["amount"] for pot in pots),
        "winners": [
            {
                "seat": winner,
                "name": engine.players[winner].name,
                "won": int(engine.players[winner].won),
            }
            for winner in sorted(winners)
        ],
        "you": {
            "seat": seat,
            "won": int(player.won),
            "net": net,
            "stack": int(player.stack),
        },
        "pots": pots,
        "refund": refund,
        "showdown": showdown,
        "cashout": _normalized(cashout) if cashout else None,
    }


__all__ = [
    "card_str",
    "describe_score",
    "event_views",
    "made_hand_view",
    "settlement_view",
    "turn_view",
    "verification_view",
]
