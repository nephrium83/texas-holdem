"""End-to-end: real Session instances play a full mental-poker deal over
the in-memory bus, with no host coordinating the deal. Proves the step-3
wiring -- driver creation, the four message types routed through
handle_message, self-delivery of a peer's own broadcasts, and card
recovery as engine Cards -- works across sessions, not just in the
driver's own harness.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.engine import Card
from holdem.p2p.session import Session
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport

try:
    from holdem.p2p import elgamal as eg
    from holdem.p2p import deal_map as dmap
    from holdem.p2p.mental_deal import derive_share
    from holdem.p2p.mental_deal_driver import label_to_card
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


def make_table(n):
    """n Sessions wired to a shared bus, seat order = [peer0..peer{n-1}], so
    seat index i is peer i. Lobby handshake bypassed."""
    bus = InMemoryBus()
    order = [f"peer{i}" for i in range(n)]
    sessions = {}
    for i, cid in enumerate(order):
        s = Session(is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
                    transport=InMemoryTransport(bus, cid))
        s.local_conn_id = cid
        s._seat_order = list(order)
        bus.register(cid, s)
        sessions[cid] = s
    return bus, sessions, order


def _begin_all(sessions, order, hand=1, button=0):
    for cid in order:
        sessions[cid].begin_hand(hand_no=hand, button=button)


def _reveal_all(sessions, order, bus, street):
    for cid in order:
        sessions[cid].reveal_board_street(street)
    bus.drain()


def _audit_all(sessions, order, bus):
    for cid in order:
        sessions[cid].open_deal_audit()
    bus.drain()


def _omniscient(deck, sessions, order, hand=1):
    """Decode every deck position using all seats' derived shares."""
    session_id = "poker|" + "|".join(order)
    xs = {i: derive_share(sessions[order[i]]._deal_master_secret, session_id, hand, i)
          for i in range(len(order))}
    out = []
    for ct in deck:
        shares = [eg.partial_decrypt(ct, xs[i]) for i in range(len(order))]
        out.append(eg.point_to_card(eg.combine(ct, shares)))
    return out


def _keys(cards):
    return [(c.v, c.s) for c in cards]


@pytest.mark.parametrize("n", [2, 3, 6])
def test_full_deal_over_sessions(n):
    bus, sessions, order = make_table(n)
    _begin_all(sessions, order)
    bus.drain()
    # all sessions converged on the same final deck
    decks = [[ct.to_hex() for ct in sessions[c]._deal_driver.deal.deck] for c in order]
    assert all(d == decks[0] for d in decks)
    # each session recovered exactly its own two hole cards, as Cards,
    # matching an omniscient decode of the shared deck
    by_pos = _omniscient(sessions[order[0]]._deal_driver.deal.deck, sessions, order)
    hp = dmap.hole_positions(0, list(range(n)))
    for i, cid in enumerate(order):
        cards = sessions[cid].deal_hole_cards
        assert all(isinstance(c, Card) for c in cards)
        expected = [label_to_card(by_pos[hp[i][0]]), label_to_card(by_pos[hp[i][1]])]
        assert _keys(cards) == _keys(expected)


def test_hole_cards_are_private_across_sessions():
    """No session recovers another seat's hole cards -- each holds only its
    own two, and the union across the table has no duplicates."""
    bus, sessions, order = make_table(3)
    _begin_all(sessions, order)
    bus.drain()
    every = []
    for cid in order:
        cards = sessions[cid].deal_hole_cards
        assert all(c is not None for c in cards)
        every.extend(_keys(cards))
    assert len(set(every)) == len(every)        # 6 distinct cards, no overlap


def test_board_reveals_street_by_street_over_sessions():
    bus, sessions, order = make_table(3)
    _begin_all(sessions, order)
    bus.drain()
    for cid in order:
        assert all(c is None for c in sessions[cid].deal_board)
    by_pos = _omniscient(sessions[order[0]]._deal_driver.deal.deck, sessions, order)
    bp = dmap.board_positions(0, [0, 1, 2])
    for street, slots in [("flop", (0, 1, 2)), ("turn", (3,)), ("river", (4,))]:
        _reveal_all(sessions, order, bus, street)
        for cid in order:
            board = sessions[cid].deal_board
            for slot in slots:
                assert board[slot] is not None
                exp = label_to_card(by_pos[bp[slot]])
                assert (board[slot].v, board[slot].s) == (exp.v, exp.s)


def test_full_hand_lifecycle_over_sessions_reaches_done():
    bus, sessions, order = make_table(4)
    _begin_all(sessions, order)
    bus.drain()
    for street in ("flop", "turn", "river"):
        _reveal_all(sessions, order, bus, street)
    _audit_all(sessions, order, bus)
    for cid in order:
        assert sessions[cid].deal_done()
        assert not sessions[cid].deal_aborted()


def test_deal_message_before_begin_hand_is_ignored():
    """A key_announce arriving before this peer has begun its hand is
    dropped (no driver yet), not an error."""
    bus, sessions, order = make_table(2)
    # only peer0 begins; peer1 has no driver and should ignore peer0's msgs
    sessions["peer0"].begin_hand(hand_no=1, button=0)
    bus.drain()
    assert sessions["peer1"]._deal_driver is None
    assert sessions["peer1"].deal_hole_cards == [None, None]


def test_seat_spoof_is_rejected():
    """A peer sending a deal message that claims a seat which is not its own
    is dropped by the receiver."""
    bus, sessions, order = make_table(3)
    _begin_all(sessions, order)
    bus.drain()
    # peer1 (seat 1) forges a message claiming to be seat 2, delivered to peer0
    forged = {"type": "deal_share", "seat_from": 2, "position": 0,
              "D_hex": "00" * 32, "dleq_hex": "00" * 64}
    before = sessions["peer0"].deal_aborted()
    sessions["peer0"].handle_message("peer1", forged)     # peer1 claims seat 2
    # dropped on the spoof check -> not fed to the driver -> no abort from it
    assert sessions["peer0"].deal_aborted() == before


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
