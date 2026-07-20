"""MentalDealDriver — bridges the MentalDeal coordinator to a transport and
the engine (L5 step 3, the testable core).

In the real peer-to-peer game each physical peer runs ONE Session with ONE
local seat, so it owns exactly ONE MentalDeal (for its own seat). This
driver wraps that instance and provides the two things the session-level
wiring needs, in a transport-agnostic, unit-testable form:

  1. A send-callback bridge. Every coordinator method returns a list of
     messages to broadcast; the driver ships them through an injected
     ``send`` callable (in the session that is transport.broadcast; in
     tests it is an in-memory bus). The session never has to remember to
     pump the coordinator's output.

  2. Card translation. The coordinator recovers cards as elgamal labels
     ("As"); the engine speaks in Card(v, s). The driver exposes the local
     seat's hole cards and the board already converted to engine Cards, so
     the session can set them on players / the board directly. This is the
     resolved Phase C model -- the coordinator sets decrypted cards on the
     engine rather than injecting a full deck, which mental poker cannot
     produce until showdown.

What this driver deliberately does NOT do: decide how the engine runs
betting in a hostless game (host-authoritative vs. per-peer replica). It
only recovers the cards; applying them and running betting is the
session's concern and is the larger, still-open part of step 3.
"""
from __future__ import annotations

from typing import Callable, List, Optional

from holdem.engine import Card
from holdem.p2p.mental_deal import MentalDeal, Phase


_RANKS = "23456789TJQKA"
_SUITS = "cdhs"


def label_to_card(label: str) -> Card:
    """Convert an elgamal card label ('As', 'Tc', ...) to an engine Card.

    v = rank index + 2 (2..14); s = suit index into "cdhs" (0..3). This
    matches engine.FULL_DECK's Card(v, s) construction and the suit order
    the deal_map bijection was verified against.
    """
    if len(label) != 2 or label[0] not in _RANKS or label[1] not in _SUITS:
        raise ValueError(f"not a card label: {label!r}")
    return Card(_RANKS.index(label[0]) + 2, _SUITS.index(label[1]))


class MentalDealDriver:
    """Drives one local seat's MentalDeal and bridges it to transport+engine."""

    def __init__(
        self,
        *,
        session_id: str,
        hand_no: int,
        local_seat: int,
        seats_in: List[int],
        button: int,
        master_secret: bytes,
        send: Callable[[dict], None],
    ):
        self.deal = MentalDeal(
            session_id=session_id,
            hand_no=hand_no,
            seat=local_seat,
            seats_in=list(seats_in),
            button=button,
            master_secret=master_secret,
        )
        self._send = send

    # -- lifecycle: each returns the raw coordinator messages (already sent) --

    def start(self) -> List[dict]:
        return self._pump(self.deal.start())

    def handle(self, msg: dict) -> List[dict]:
        return self._pump(self.deal.handle(msg))

    def reveal_street(self, street: str) -> List[dict]:
        return self._pump(self.deal.reveal_street(street))

    def open_audit(self) -> List[dict]:
        return self._pump(self.deal.open_audit())

    def _pump(self, msgs: List[dict]) -> List[dict]:
        for m in msgs:
            self._send(m)
        return msgs

    # -- recovered cards, as engine Cards --

    @property
    def hole_cards(self) -> List[Optional[Card]]:
        """This seat's two hole cards as Cards (None until recovered)."""
        return [label_to_card(c) if c else None for c in self.deal.hole_cards]

    @property
    def board(self) -> List[Optional[Card]]:
        """The board as Cards, filling street by street (None until revealed)."""
        return [label_to_card(c) if c else None for c in self.deal.board]

    # -- status passthroughs --

    @property
    def phase(self) -> Phase:
        return self.deal.phase

    def hole_complete(self) -> bool:
        return self.deal.hole_complete()

    def board_complete(self) -> bool:
        return self.deal.board_complete()

    def is_done(self) -> bool:
        return self.deal.is_done()

    def aborted(self) -> bool:
        return self.deal.phase == Phase.ABORTED

    @property
    def bad_seat(self) -> Optional[int]:
        return self.deal.bad_seat

    @property
    def abort_reason(self) -> Optional[str]:
        return self.deal.abort_reason

    @property
    def audit_report(self):
        return self.deal.audit_report


__all__ = ["MentalDealDriver", "label_to_card"]
