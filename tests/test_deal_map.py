"""Pins holdem/p2p/deal_map.py against the ENGINE's real ordering.

The point of these tests is ground truth: the position map and the card
index bijection are validated against holdem.engine.FULL_DECK,
holdem.p2p.elgamal.CARDS, and an actual Engine.start_hand deal -- not
against a restatement of the rules. A silent mismatch here would deal
wrong cards, so it is checked directly.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p import deal_map as dm
from holdem.engine import Engine, Player, Deck, FULL_DECK

# elgamal import may fail if libsodium absent; the card-label/order tests
# that need it are guarded, but the index-translation tests do not need it.
try:
    from holdem.p2p import elgamal as eg
    _HAVE_EG = True
except RuntimeError:
    _HAVE_EG = False


# ------------------------------------------------------ card index bijection

def test_index_translation_is_bijection():
    fwd = [dm.elgamal_to_engine_index(i) for i in range(52)]
    assert sorted(fwd) == list(range(52))            # bijection onto 0..51
    for i in range(52):
        assert dm.engine_to_elgamal_index(dm.elgamal_to_engine_index(i)) == i
        assert dm.elgamal_to_engine_index(dm.engine_to_elgamal_index(i)) == i


def test_index_translation_preserves_card_identity():
    """elgamal card i and engine FULL_DECK[translate(i)] are the SAME card."""
    rank_to_v = {r: v for v, r in zip(range(2, 15), "23456789TJQKA")}
    suit_to_idx = {"c": 0, "d": 1, "h": 2, "s": 3}
    for i in range(52):
        label = dm.card_label(i)
        fd = FULL_DECK[dm.elgamal_to_engine_index(i)]
        assert fd.v == rank_to_v[label[0]]
        assert fd.s == suit_to_idx[label[1]]


def test_translation_fixed_points_and_divergence():
    assert dm.elgamal_to_engine_index(0) == 0        # 2c
    assert dm.elgamal_to_engine_index(51) == 51      # As
    # they genuinely differ in between (not the identity map)
    assert any(dm.elgamal_to_engine_index(i) != i for i in range(1, 51))


@pytest.mark.skipif(not _HAVE_EG, reason="libsodium/elgamal unavailable")
def test_card_label_matches_elgamal_cards():
    for i in range(52):
        assert dm.card_label(i) == eg.CARDS[i]


def test_index_range_guards():
    for bad in (-1, 52, 100):
        with pytest.raises(ValueError):
            dm.elgamal_to_engine_index(bad)
        with pytest.raises(ValueError):
            dm.engine_to_elgamal_index(bad)
        with pytest.raises(ValueError):
            dm.card_label(bad)


# ------------------------------------------------------ seat deal order

def test_seat_order_full_ring():
    # 6 seats, button at 0 -> order starts at seat 1, wraps
    assert dm.seat_deal_order(0, range(6)) == [1, 2, 3, 4, 5, 0]
    # button at 5 -> starts at 0
    assert dm.seat_deal_order(5, range(6)) == [0, 1, 2, 3, 4, 5]


def test_seat_order_skips_vacated():
    # seats 0,2,4 in the hand, button at 4 -> next seated left of 4 is 0, then 2
    assert dm.seat_deal_order(4, [0, 2, 4]) == [0, 2, 4]
    assert dm.seat_deal_order(0, [0, 2, 4]) == [2, 4, 0]


def test_seat_order_dead_button():
    # button on a vacated seat (dead-button rule): still steps left from it
    assert dm.seat_deal_order(1, [0, 2, 3]) == [2, 3, 0]


def test_seat_order_heads_up():
    assert dm.seat_deal_order(0, [0, 1]) == [1, 0]


# ------------------------------------------------------ deal map shape

def test_deal_map_length_and_structure():
    m = 6
    positions = dm.deal_map(0, range(m))
    assert len(positions) == 2 * m + 5
    # first 2m are hole (two passes), last 5 are board
    holes = positions[: 2 * m]
    board = positions[2 * m:]
    assert all(d.kind == dm.HOLE for d in holes)
    assert all(d.kind == dm.BOARD for d in board)
    # two passes: positions 0..m-1 ordinal 0, m..2m-1 ordinal 1, same seat order
    order = dm.seat_deal_order(0, range(m))
    for k, seat in enumerate(order):
        assert holes[k] == dm.Destination(dm.HOLE, seat, 0)
        assert holes[m + k] == dm.Destination(dm.HOLE, seat, 1)
    # flop is stored reversed by the engine: served order maps to slots 2,1,0
    assert board[0] == dm.Destination(dm.BOARD, -1, 2)
    assert board[1] == dm.Destination(dm.BOARD, -1, 1)
    assert board[2] == dm.Destination(dm.BOARD, -1, 0)
    assert board[3] == dm.Destination(dm.BOARD, -1, 3)   # turn
    assert board[4] == dm.Destination(dm.BOARD, -1, 4)   # river


def test_hole_and_board_position_helpers():
    hp = dm.hole_positions(0, range(4))
    order = dm.seat_deal_order(0, range(4))       # [1,2,3,0]
    # seat order[0] gets positions 0 (pass1) and 4 (pass2) for m=4
    assert hp[order[0]] == [0, 4]
    assert hp[order[1]] == [1, 5]
    # flop stored reversed: board slot k -> deck position
    assert dm.board_positions(0, range(4)) == [10, 9, 8, 11, 12]


# ------------------------------------------------------ THE ground-truth test

def _deal_from_known_deck(n_players, button):
    """Deal a hand with a deck injected in canonical 0..51 FULL_DECK order,
    so we can read exactly which FULL_DECK index landed where.

    NOTE: the engine moves the button during start_hand (blinds / dead-button
    rule), so callers must use ``e.button`` (the actual dealing button) when
    calling deal_map, not the value passed in here.
    """
    players = [Player(i, f"P{i}", 1000) for i in range(n_players)]
    e = Engine(players, sb=10, bb=20, structure="No-Limit")
    e.button = button
    # inject FULL_DECK in index order 0..51; Deck.from_indices serves [0] first
    e.start_hand(deck=Deck.from_indices(list(range(52))))
    return e


def test_deal_map_predicts_engine_hole_cards():
    """deal_map says position p -> seat/ordinal; the engine, dealt an
    identity-ordered deck, must put FULL_DECK[p] exactly there."""
    for n_players, button in [(2, 0), (3, 0), (6, 2), (9, 5)]:
        e = _deal_from_known_deck(n_players, button)
        seats_in = [p.idx for p in e.players if p.in_seat]
        positions = dm.deal_map(e.button, seats_in)   # engine's actual button
        # engine dealt position p as FULL_DECK[p]; find each hole card's pos
        for dest_pos, dest in enumerate(positions):
            if dest.kind != dm.HOLE:
                continue
            expected_card = FULL_DECK[dest_pos]      # served p-th
            actual_card = e.players[dest.seat].hole[dest.ordinal]
            assert actual_card.v == expected_card.v and actual_card.s == expected_card.s, (
                f"n={n_players} btn={button} pos={dest_pos} seat={dest.seat} "
                f"ord={dest.ordinal}: expected {expected_card}, got {actual_card}")


def test_deal_map_predicts_engine_board():
    """After running out all streets, board positions must hold FULL_DECK[p]."""
    for n_players, button in [(2, 0), (6, 2), (9, 5)]:
        e = _deal_from_known_deck(n_players, button)
        seats_in = [p.idx for p in e.players if p.in_seat]
        bpos = dm.board_positions(e.button, seats_in)   # engine's actual button
        # advance through the streets to fill the board
        while len(e.board) < 5:
            e.next_street()
        for slot, pos in enumerate(bpos):
            expected = FULL_DECK[pos]
            actual = e.board[slot]
            assert actual.v == expected.v and actual.s == expected.s, (
                f"n={n_players} btn={button} board slot {slot} pos={pos}: "
                f"expected {expected}, got {actual}")


if __name__ == "__main__":
    passed = total = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        marks = getattr(fn, "pytestmark", [])
        skip = any(m.name == "skipif" and m.args and m.args[0] for m in marks)
        if skip:
            print(f"  {name}: SKIP")
            continue
        total += 1
        try:
            fn()
            passed += 1
            print(f"  {name}: ok")
        except Exception as exc:
            print(f"  {name}: FAIL - {type(exc).__name__}: {exc}")
    print(f"{passed}/{total} passed")
