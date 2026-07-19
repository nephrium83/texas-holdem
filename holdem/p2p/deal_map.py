"""Canonical deal map + card-index translation (L5 step 1).

Two pure, deterministic, crypto-free pieces the mental-poker coordinator
needs. Both are computed identically by every peer (the hostless design
requires that), and both are pinned by tests against the engine's own
ordering so a silent mismatch cannot deal the wrong cards.

1. deal_map(button, seats_in) -> the mapping from DECK POSITION (the order
   cards are consumed from the shuffled deck) to DESTINATION (a specific
   seat's hole card, or a board slot). This mirrors holdem.engine's deal
   exactly: hole cards dealt in TWO passes of one card each, seat order
   starting one seat LEFT of the button and skipping vacated seats; then
   flop (3), turn (1), river (1); NO burn cards (the engine burns none).

2. elgamal_to_engine_index / engine_to_elgamal_index -- the bijection
   between mental-poker's suit-major card numbering (elgamal.CARDS) and
   the engine's rank-major numbering (engine.FULL_DECK). Required because
   a card recovered by threshold decryption is an elgamal card label, but
   any interaction with engine deck indices (e.g. Deck.from_indices) is in
   FULL_DECK space. They coincide only at index 0 (2c) and 51 (As).

Deck-position order (how the shuffled 52-card deck is consumed)
--------------------------------------------------------------
Position p = 0,1,2,... maps to, in order:
    hole round 1:  seat order[0], order[1], ..., order[m-1]
    hole round 2:  seat order[0], order[1], ..., order[m-1]
    flop:          board[2], board[1], board[0]   (engine stores flop reversed)
    turn:          board[3]
    river:         board[4]
where m = number of seated players and `order` is the engine's deal order
(left of button, wrapping, in-seat only). Total positions used = 2*m + 5.

This matches engine.Deck.deal() consuming the deck and
Deck.from_indices() serving indices[0] first, so deck position p is served
as the p-th card dealt.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Literal, Sequence

# Card orderings, mirrored from their sources so this module has no import
# cost from the engine. Verified equal to engine.FULL_DECK / elgamal.CARDS
# by tests (test_deal_map), which import the real objects and compare.
_RANKS = "23456789TJQKA"          # rank order (v = index + 2)
_SUITS = "cdhs"                     # suit order used by BOTH numbering schemes

# elgamal.CARDS: suit-major  ->  [r+s for s in "cdhs" for r in _RANKS]
# engine.FULL_DECK: rank-major -> Card(v,s) for v in 2..14 for s in 0..3
#   engine index = (v-2)*4 + s = rank_idx*4 + suit_idx
#   elgamal index = suit_idx*13 + rank_idx


BOARD = "board"
HOLE = "hole"


@dataclass(frozen=True)
class Destination:
    """Where a dealt card goes.

    kind == 'hole': ``seat`` is the seat index, ``ordinal`` is 0 or 1
        (first or second hole card).
    kind == 'board': ``ordinal`` is 0..4 (flop0, flop1, flop2, turn, river);
        ``seat`` is -1.
    """
    kind: Literal["hole", "board"]
    seat: int
    ordinal: int


def seat_deal_order(button: int, seats_in: Sequence[int]) -> List[int]:
    """The engine's hole-deal seat order: left of button, wrapping, seated.

    ``seats_in`` is the set of seat indices that are in the hand (the
    engine's ``in_seat`` players). ``button`` is a seat index; it need not
    be in ``seats_in`` (dead-button rule can vacate it). Returns the seats
    in the order they receive cards.
    """
    seated = set(seats_in)
    if not seated:
        return []
    # highest seat index present bounds the ring we step around
    ring = max(max(seated), button) + 1
    order: List[int] = []
    j = button
    for _ in range(ring):
        j = (j + 1) % ring
        if j in seated:
            order.append(j)
        if len(order) == len(seated):
            break
    return order


def deal_map(button: int, seats_in: Sequence[int]) -> List[Destination]:
    """Deck position -> Destination, mirroring engine.start_hand's deal.

    Returns a list indexed by deck position: index p is the destination of
    the p-th card served from the shuffled deck. Length is 2*m + 5 where
    m = len(seats_in). Positions 0..2m-1 are hole cards (two passes),
    2m..2m+4 are flop(3)/turn(1)/river(1).
    """
    order = seat_deal_order(button, seats_in)
    positions: List[Destination] = []
    # two passes of one card each
    for ordinal in (0, 1):
        for seat in order:
            positions.append(Destination(kind=HOLE, seat=seat, ordinal=ordinal))
    # board: flop(3), turn(1), river(1).
    # The engine deals the flop with deck.deal(3), which pops 3 cards off
    # the end of the deck and returns them in REVERSE of the order they
    # were served -- so the three flop deck-positions map to board slots
    # 0,1,2 in reverse: the last-served flop card is board[0]. Turn and
    # river are single cards, unaffected.
    # served order (3 flop cards) -> board slots 2,1,0 (engine stores reversed)
    positions.append(Destination(kind=BOARD, seat=-1, ordinal=2))   # served 1st -> slot 2
    positions.append(Destination(kind=BOARD, seat=-1, ordinal=1))   # served 2nd -> slot 1
    positions.append(Destination(kind=BOARD, seat=-1, ordinal=0))   # served 3rd -> slot 0
    positions.append(Destination(kind=BOARD, seat=-1, ordinal=3))   # turn
    positions.append(Destination(kind=BOARD, seat=-1, ordinal=4))   # river
    return positions


def hole_positions(button: int, seats_in: Sequence[int]) -> Dict[int, List[int]]:
    """seat -> [deck position of its first hole card, of its second]."""
    out: Dict[int, List[int]] = {}
    for pos, dest in enumerate(deal_map(button, seats_in)):
        if dest.kind == HOLE:
            out.setdefault(dest.seat, [None, None])
            out[dest.seat][dest.ordinal] = pos
    return out


def board_positions(button: int, seats_in: Sequence[int]) -> List[int]:
    """[deck position of flop0, flop1, flop2, turn, river] (by board slot).

    Returns deck positions ordered by board slot (ordinal 0..4), i.e.
    board_positions(...)[k] is the deck position whose decrypted card is
    self.board[k]. Because the engine stores the flop reversed relative to
    deal order, the flop entries are NOT contiguous-ascending.
    """
    slot_to_pos = {dest.ordinal: pos
                   for pos, dest in enumerate(deal_map(button, seats_in))
                   if dest.kind == BOARD}
    return [slot_to_pos[k] for k in range(5)]


# -------------------------------------------------------------- card index

def elgamal_to_engine_index(idx: int) -> int:
    """Suit-major (elgamal.CARDS) index -> rank-major (engine.FULL_DECK)."""
    if not 0 <= idx < 52:
        raise ValueError(f"card index out of range: {idx}")
    suit_idx, rank_idx = divmod(idx, 13)          # elgamal = suit*13 + rank
    return rank_idx * 4 + suit_idx                 # engine = rank*4 + suit


def engine_to_elgamal_index(idx: int) -> int:
    """Rank-major (engine.FULL_DECK) index -> suit-major (elgamal.CARDS)."""
    if not 0 <= idx < 52:
        raise ValueError(f"card index out of range: {idx}")
    rank_idx, suit_idx = divmod(idx, 4)            # engine = rank*4 + suit
    return suit_idx * 13 + rank_idx                # elgamal = suit*13 + rank


def card_label(elgamal_idx: int) -> str:
    """The two-char label ('As') for an elgamal card index."""
    if not 0 <= elgamal_idx < 52:
        raise ValueError(f"card index out of range: {elgamal_idx}")
    suit_idx, rank_idx = divmod(elgamal_idx, 13)
    return _RANKS[rank_idx] + _SUITS[suit_idx]


__all__ = [
    "Destination", "HOLE", "BOARD",
    "seat_deal_order", "deal_map", "hole_positions", "board_positions",
    "elgamal_to_engine_index", "engine_to_elgamal_index", "card_label",
]
