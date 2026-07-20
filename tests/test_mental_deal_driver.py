"""Pins holdem/p2p/mental_deal_driver.py -- the coordinator<->transport
bridge and elgamal-label -> engine-Card translation.

An in-memory bus runs N drivers through a full hand (the real game has one
driver per peer; the bus simulates the broadcast fabric between them).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.engine import Card, FULL_DECK

try:
    from holdem.p2p import elgamal as eg
    from holdem.p2p import deal_map as dmap
    from holdem.p2p.mental_deal_driver import MentalDealDriver, label_to_card
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


def _secrets(seats):
    return {s: f"master-secret-of-seat-{s}".encode() for s in seats}


def _run_drivers(seats, ms, session="s", hand=1, button=0):
    """One driver per seat, all sharing an in-memory broadcast bus; run to
    the fixed point (through the hole deal)."""
    bus = []
    drivers = {
        s: MentalDealDriver(session_id=session, hand_no=hand, local_seat=s,
                            seats_in=list(seats), button=button,
                            master_secret=ms[s], send=bus.append)
        for s in seats
    }
    for s in seats:
        drivers[s].start()
    while bus:
        m = bus.pop(0)
        for s in seats:
            drivers[s].handle(dict(m))
    return drivers, bus


def _reveal(drivers, seats, street, bus):
    for s in seats:
        drivers[s].reveal_street(street)
    while bus:
        m = bus.pop(0)
        for s in seats:
            drivers[s].handle(dict(m))


def _open_audit(drivers, seats, bus):
    for s in seats:
        drivers[s].open_audit()
    while bus:
        m = bus.pop(0)
        for s in seats:
            drivers[s].handle(dict(m))


# ------------------------------------------------------ label -> Card

def test_label_to_card_matches_full_deck():
    """label_to_card must agree with the engine's own numbering via the
    deal_map bijection, card for card."""
    for idx, label in enumerate(eg.CARDS):
        card = label_to_card(label)
        fd = FULL_DECK[dmap.elgamal_to_engine_index(idx)]
        assert isinstance(card, Card)
        assert card.v == fd.v and card.s == fd.s


def test_label_to_card_rejects_bad():
    for bad in ("", "A", "Xs", "2z", "AsA"):
        with pytest.raises(ValueError):
            label_to_card(bad)


# ------------------------------------------------------ the deal, as Cards

@pytest.mark.parametrize("n", [2, 3, 6, 9])
def test_drivers_recover_hole_cards_as_cards(n):
    seats = list(range(n))
    ms = _secrets(seats)
    drivers, _ = _run_drivers(seats, ms)
    for s in seats:
        assert drivers[s].hole_complete()
        cards = drivers[s].hole_cards
        assert all(isinstance(c, Card) for c in cards)
        # match the coordinator's own recovered labels, translated
        labels = drivers[s].deal.hole_cards
        for card, label in zip(cards, labels):
            assert str(card) == _canonical(label)


def _canonical(label):
    """Render a label the way engine Card.__str__ would, for comparison."""
    return str(label_to_card(label))


def test_all_hole_cards_distinct_real_cards():
    seats = [0, 1, 2, 3, 4, 5]
    drivers, _ = _run_drivers(seats, _secrets(seats))
    every = []
    for s in seats:
        every.extend(drivers[s].hole_cards)
    assert all(isinstance(c, Card) for c in every)
    valid = {(c.v, c.s) for c in FULL_DECK}      # Card has no __eq__; compare by value
    assert all((c.v, c.s) in valid for c in every)
    keys = [(c.v, c.s) for c in every]
    assert len(set(keys)) == len(keys)          # no card dealt twice


def test_board_reveals_as_cards():
    seats = [0, 1, 2, 3]
    ms = _secrets(seats)
    drivers, bus = _run_drivers(seats, ms)
    for s in seats:
        assert all(c is None for c in drivers[s].board)
    for street in ("flop", "turn", "river"):
        _reveal(drivers, seats, street, bus)
    for s in seats:
        assert drivers[s].board_complete()
        assert all(isinstance(c, Card) for c in drivers[s].board)
        keys = [(c.v, c.s) for c in drivers[s].board]
        assert len(set(keys)) == 5


def test_driver_full_lifecycle_reaches_done():
    seats = [0, 1, 2, 3]
    ms = _secrets(seats)
    drivers, bus = _run_drivers(seats, ms)
    for street in ("flop", "turn", "river"):
        _reveal(drivers, seats, street, bus)
    _open_audit(drivers, seats, bus)
    for s in seats:
        assert drivers[s].is_done()
        assert not drivers[s].aborted()
        assert drivers[s].audit_report.ok


# ------------------------------------------------------ the send bridge

def test_send_callback_receives_messages():
    sent = []
    d = MentalDealDriver(session_id="s", hand_no=1, local_seat=0,
                         seats_in=[0, 1, 2], button=0,
                         master_secret=b"m", send=sent.append)
    returned = d.start()
    # start() emits the key_announce, which must have gone through send
    assert len(sent) == 1
    assert sent[0]["type"] == "key_announce"
    assert returned == sent                     # returns what it sent


def test_hole_cards_none_before_deal():
    d = MentalDealDriver(session_id="s", hand_no=1, local_seat=0,
                         seats_in=[0, 1], button=0,
                         master_secret=b"m", send=lambda m: None)
    assert d.hole_cards == [None, None]
    assert not d.hole_complete()


if __name__ == "__main__":
    passed = total = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        marks = getattr(fn, "pytestmark", [])
        params = None
        for m in marks:
            if m.name == "parametrize":
                params = m.args[1]
        cases = params if params else [None]
        for c in cases:
            total += 1
            try:
                fn(c) if params else fn()
                passed += 1
                print(f"  {name}{'['+str(c)+']' if params else ''}: ok")
            except Exception as exc:
                print(f"  {name}{'['+str(c)+']' if params else ''}: FAIL - {exc}")
    print(f"{passed}/{total} passed")
