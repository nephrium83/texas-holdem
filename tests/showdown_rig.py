"""Known showdowns for Session tests that need a particular seat to bust.

Sessions on the in-memory bus deal real mental-poker hands, so who wins an
all-in is random, and a test that needed a bust had to retry until one
happened. rig_showdowns() patches ReplicaTable for one test -- every replica
in the process alike, so the replicas still agree -- so that the cards it
scores are fixed: a board with no pair, flush or straight on it, and a
pocket pair per ranked seat, aces for the seat ranked first, then kings,
queens, jacks and tens. The deal still runs and still recovers its own
cards; only what the replica scores is replaced.
"""
from __future__ import annotations

from holdem.engine import Card
from holdem.p2p.replica_table import ReplicaTable

BOARD = ["2c", "7d", "9h", "4s", "3d"]
PAIRS = "AKQJT"
# Hole cards for seats a test does not rank: no rank on BOARD (no pair), no
# 5 with a 6 or A with a 5 (no straight), clubs/diamonds only (no flush, and
# never the spade/heart ranked pairs). High card at best.
FILLERS = [("8c", "Jd"), ("8d", "Jc"), ("Tc", "Qd"), ("Td", "Qc"),
           ("Kc", "5d"), ("Kd", "5c"), ("6c", "Ad"), ("6d", "Ac")]
_STREET = {"preflop": slice(0, 3), "flop": slice(3, 4), "turn": slice(4, 5)}


def card(label: str) -> Card:
    """'As' -> Card."""
    return Card("23456789TJQKA".index(label[0]) + 2, "cdhs".index(label[1]))


def rig_showdowns(monkeypatch):
    """Patch for this test. Returns rank(*seats), which sets the order every
    later showdown is won in, best first. An unranked seat is given filler
    (high card only, so it loses to every ranked pair) rather than the cards
    it was dealt, which could collide with the fixed board -- so rank every
    seat that can reach a showdown."""
    ranked: list = []
    set_holes = ReplicaTable.set_all_holes
    advance = ReplicaTable.advance_street

    def fixed_holes(self, holes_by_seat):
        # An unranked seat used to keep the cards it was really dealt. Those
        # come from the real random deal while BOARD is fixed, so they could
        # collide with it (seat 0 holding 4s 7d on a 2c 7d 9h 4s 3d board):
        # an impossible hand, which ReplicaTable now refuses (audit #65).
        # Unranked seats get FILLER instead -- distinct, off the board and
        # the ranked pairs, high-card only -- so they still lose to every
        # ranked pocket pair and no rigged outcome changes.
        unranked = sorted(s for s in holes_by_seat if s not in ranked)
        filler = dict(zip(unranked, FILLERS))
        set_holes(self, {
            seat: ([card(PAIRS[ranked.index(seat)] + "s"),
                    card(PAIRS[ranked.index(seat)] + "h")]
                   if seat in ranked else [card(c) for c in filler[seat]])
            for seat in holes_by_seat})

    def fixed_board(self, cards):
        advance(self, [card(c) for c in BOARD[_STREET[self.engine.street]]])

    monkeypatch.setattr(ReplicaTable, "set_all_holes", fixed_holes)
    monkeypatch.setattr(ReplicaTable, "advance_street", fixed_board)

    def rank(*best_first):
        ranked[:] = best_first
    return rank
