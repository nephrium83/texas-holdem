"""ReplicaTable — one peer's replica engine for hostless betting (L5 step 3).

The hostless-betting model: every peer runs its OWN copy of the holdem
engine. Player actions (fold/call/raise) are broadcast; every replica
applies them in the same total order; because the engine's betting path is
deterministic, all replicas stay in perfect sync with no authoritative
host. This module is that replica: transport-agnostic and unit-testable,
exactly like the MentalDeal coordinator. The session layer moves the
messages; ReplicaTable decides and applies.

How the pieces fit (the resolved model):
  * The engine is constructed identically on every peer from the shared
    table config, and start_hand() runs with a DUMMY deck -- the real
    cards exist only inside the mental-poker deal. The dummy deal gives
    the engine everything betting needs (blinds, button move, first
    actor); the dummy cards themselves are never read by betting logic.
  * This seat's real hole cards (recovered by the mental deal) overwrite
    the local replica's own seat only. Other seats keep dummy cards until
    the audit -- replicas legitimately differ in hole-card contents, and
    the state digest deliberately excludes them.
  * Board streets: when a betting round closes, the mental deal reveals
    the street; the recovered Cards are PRE-LOADED onto the dummy deck's
    tail so engine.next_street() deals exactly them. The engine stays
    truthful -- its board is the real board.
  * Showdown: the post-hand audit reveals every seat's true cards; they
    are injected onto all contested seats before settle(runs=1). A hand
    that ends by folds needs no cards at all (settle skips scoring).
    settle is NEVER allowed to complete a board from the dummy deck --
    ReplicaTable requires 5 real board cards first. Run-it-twice deals
    from the deck and is therefore impossible under mental poker; runs is
    pinned to 1 (v2 backlog).

Total ordering. Each action carries (hand_no, seq). A replica applies seq
n only when it has applied exactly n prior actions AND the acting seat is
its own engine's current actor. Later seqs buffer (network reordering);
earlier seqs drop (duplicates); a seq-correct but invalid action is
rejected identically by every replica (same state, same rules) without
advancing the sequence. Only one seat can validly act at any moment, so
there is nothing to arbitrate -- the sequence numbers just pin the order
delivery must respect.

Desync detection. state_digest() hashes every piece of betting-relevant
state. Replicas MUST agree after every applied action; the session layer
can exchange digests to detect (and void on) divergence. Hole cards are
excluded on purpose -- they differ across replicas until the audit.
"""
from __future__ import annotations

import hashlib
from typing import Dict, List, Optional

from holdem.engine import Card, Deck, Engine, IllegalAction, Player


PHASE_BETTING = "betting"          # a betting round is open
PHASE_STREET_OVER = "street_over"  # round closed; next street must be revealed
PHASE_SHOWDOWN = "showdown"        # river betting closed; audit + settle
PHASE_HAND_OVER = "hand_over"      # folded down to one; settle now
PHASE_SETTLED = "settled"

# Forward window for out-of-order actions, and the buffer's capacity.
MAX_PENDING = 64


class UnverifiedHolesError(RuntimeError):
    """A contested showdown would be scored on dummy (unaudited) holes."""


def _hand_seed(session_id: str, hand_no: int) -> int:
    h = hashlib.sha256(f"replica.v1|{session_id}|{hand_no}".encode()).digest()
    return int.from_bytes(h[:8], "big")


class ChipConservationError(RuntimeError):
    """settle() left the table holding a different number of chips than
    the hand was dealt with. The session voids the hand on this rather
    than carrying created or destroyed chips into the next one."""


class ReplicaTable:
    """One peer's deterministic replica of the table for a single hand."""

    def __init__(self, *, session_id: str, hand_no: int,
                 names: List[str], stacks: List[int],
                 sb: int, bb: int, structure: str = "No-Limit"):
        if len(names) != len(stacks):
            raise ValueError("names and stacks must align")
        import random as _random
        players = [Player(i, names[i], stacks[i], human=True)
                   for i in range(len(names))]
        # Seeded rng, identical on every replica (belt and braces: the
        # betting path never consults it, but if anything ever does, the
        # replicas still agree).
        rng = _random.Random(_hand_seed(session_id, hand_no))
        self.engine = Engine(players, sb=sb, bb=bb, structure=structure,
                             rng=rng)
        self.hand_no = hand_no
        # Every chip at the table when the hand was dealt; settle() must
        # hand back exactly this many (see finish).
        self.start_total = sum(stacks)
        self.next_seq = 0
        self.phase = PHASE_BETTING
        self._pending: Dict[int, tuple] = {}     # seq -> (seat, action, amount)
        self.result: Optional[dict] = None
        # Seats whose REAL hole cards are in the engine (audit #65): the local
        # seat via set_own_hole, the rest via set_all_holes after the audit.
        # Every other seat still holds the dummy deal's cards.
        self._real_holes: set = set()

    # ------------------------------------------------------------- lifecycle

    def start_hand(self, button: int, *, bb_seat: Optional[int] = None,
                   sb_seat: Optional[int] = None) -> bool:
        """Deal the hand with a dummy deck. `button` is the pre-move button;
        the engine advances it -- read `self.button` AFTER this call and use
        THAT for the mental deal, so deal_map matches the engine's order.

        For hand 1 pass only `button` (the engine's first-hand branch
        derives positions from it). For every LATER hand of a continuous
        session also pass the previous hand's played (bb_seat, sb_seat)
        from `positions`, so the engine's dead-button rule advances the
        chain exactly as a live table would: the BB moves one eligible
        seat; the SB and button trail it and may land on busted seats.
        Returns False when fewer than two seats can be dealt (the session
        is over)."""
        self.engine.button = button
        if bb_seat is not None:
            self.engine.bb_seat = bb_seat
        if sb_seat is not None:
            self.engine.sb_seat = sb_seat
        ok = self.engine.start_hand(deck=Deck.from_indices(list(range(52))))
        self._recompute_phase()
        return bool(ok)

    @property
    def button(self) -> int:
        return self.engine.button

    @property
    def positions(self) -> tuple:
        """(bb_seat, sb_seat, button) as played -- the dead-button chain
        state to seed into the NEXT hand's replica."""
        e = self.engine
        return (e.bb_seat, e.sb_seat, e.button)

    @property
    def stacks(self) -> List[int]:
        """Current stacks by seat (post-settle: the next hand's inputs)."""
        return [p.stack for p in self.engine.players]

    @property
    def seats_dealt(self) -> List[int]:
        """Seat indices dealt into this hand (busted seats excluded by the
        engine's _dealt rule)."""
        return [p.idx for p in self.engine.players if p.in_seat]

    @property
    def actor(self) -> Optional[int]:
        return self.engine.actor

    def set_own_hole(self, seat: int, cards: List[Card]) -> None:
        """Overwrite ONE seat's dummy hole with its real recovered cards
        (the local seat; other replicas keep dummies until the audit)."""
        if len(cards) != 2 or any(c is None for c in cards):
            raise ValueError("need two recovered cards")
        self.engine.players[seat].hole = list(cards)
        self._real_holes.add(seat)

    # ------------------------------------------------------------- actions

    def apply_action(self, seq: int, seat: int, action: str,
                     amount: int = 0) -> str:
        """Apply (or buffer/drop/reject) one broadcast action.

        Returns "applied", "buffered", "stale", or "rejected". Every
        replica, holding the same state, returns the same verdict for the
        same message -- rejection never advances the sequence, so a
        malformed action from the current actor stalls the hand (session
        layer times out and voids) rather than forking state.
        """
        if seq < self.next_seq:
            return "stale"
        if seq > self.next_seq:
            # Audit #67: "buffered" used to be returned even when nothing was
            # stored (buffer full), so a legitimate reordered action vanished
            # while its caller was told it was kept. Now the verdict says what
            # happened: "buffered" only if stored (or already stored
            # identically), "overflow" when it is beyond the forward window
            # or the buffer is full, "rejected" for a conflicting duplicate.
            entry = (seat, action, amount)
            if seq in self._pending:
                return "buffered" if self._pending[seq] == entry else "rejected"
            if seq - self.next_seq > MAX_PENDING or len(self._pending) >= MAX_PENDING:
                return "overflow"
            self._pending[seq] = entry
            return "buffered"
        verdict = self._apply_now(seat, action, amount)
        if verdict == "applied":
            self.next_seq += 1
            self._drain_pending()
        return verdict

    def _apply_now(self, seat: int, action: str, amount: int) -> str:
        e = self.engine
        if self.phase != PHASE_BETTING:
            return "rejected"
        if e.actor is None or seat != e.actor:
            return "rejected"
        if action not in ("fold", "call", "raise"):
            return "rejected"
        if action == "raise" and not e.legal(seat)["can_raise"]:
            return "rejected"
        try:
            # The engine is the authority on legality -- including the raise
            # range, which it now REJECTS rather than clamps (rules profile
            # gap C2). Its refusal leaves state untouched; every replica
            # reaches the same verdict for the same message.
            e.act(seat, action, int(amount))
        except IllegalAction:
            return "rejected"
        self._recompute_phase()
        return "applied"

    def _drain_pending(self) -> None:
        while self.next_seq in self._pending:
            seat, action, amount = self._pending.pop(self.next_seq)
            if self._apply_now(seat, action, amount) != "applied":
                break                     # invalid buffered msg: stop, don't skip
            self.next_seq += 1

    # ------------------------------------------------------------- streets

    def advance_street(self, cards: List[Card]) -> None:
        """Advance to the next street, dealing exactly the REAL revealed
        `cards` (board-slot order: flop [b0,b1,b2]; turn [b3]; river [b4]).
        They are appended to the dummy deck's tail, which deal() pops, so
        the engine's board is the true board."""
        if self.phase != PHASE_STREET_OVER:
            raise RuntimeError(f"cannot advance street in phase {self.phase}")
        need = {"preflop": 3, "flop": 1, "turn": 1}.get(self.engine.street)
        if need is None:
            raise RuntimeError(f"no street to deal from {self.engine.street}")
        if len(cards) != need or any(c is None for c in cards):
            raise ValueError(f"need {need} recovered cards")
        self.engine.deck.cards.extend(cards)   # deal(n) pops these exact cards
        self.engine.next_street()
        self._recompute_phase()

    def _recompute_phase(self) -> None:
        e = self.engine
        if self.phase == PHASE_SETTLED:
            return
        if len(e.contested()) <= 1:
            self.phase = PHASE_HAND_OVER
        elif e.need_to_act:
            self.phase = PHASE_BETTING
        elif e.street == "river" or e.street == "showdown":
            self.phase = PHASE_SHOWDOWN
        else:
            self.phase = PHASE_STREET_OVER

    # ------------------------------------------------------------- showdown

    def set_all_holes(self, holes_by_seat: Dict[int, List[Card]]) -> None:
        """Inject the audit-revealed true hole cards for every seat given
        (at minimum every contested seat) before settling a showdown."""
        seen = {(c.v, c.s) for c in self.engine.board}
        for seat, cards in holes_by_seat.items():
            if not 0 <= seat < len(self.engine.players):
                raise ValueError(f"seat {seat}: no such seat")
            if len(cards) != 2 or any(c is None for c in cards):
                raise ValueError(f"seat {seat}: need two cards")
            for c in cards:
                if (c.v, c.s) in seen:
                    raise ValueError(f"seat {seat}: card {c} is dealt twice")
                seen.add((c.v, c.s))
        for seat, cards in holes_by_seat.items():
            self.engine.players[seat].hole = list(cards)
            self._real_holes.add(seat)

    def finish(self, force_tabled: bool = False) -> dict:
        """Settle the hand (runs pinned to 1 -- run-it-twice deals from the
        deck and cannot exist under mental poker). For a contested showdown
        the full 5-card real board must already be in place; settle is
        never allowed to invent board cards from the dummy deck.

        Chips are counted after settling: every pot and refund must land
        back in a stack, so the stacks sum to start_total. A mismatch raises
        ChipConservationError with no result recorded and the phase
        unchanged, and the session voids the hand. If every replica fails
        the same way, it is redealt from the stacks it started with. If the
        others settled it, the table ends instead, because the settlements
        disagree (Session._settlement_disagrees). The engine has already
        paid out by then: this replica's stacks and state_digest() show the
        non-conserving chips and mean nothing. Read the session's
        last_settled_stacks."""
        if self.phase not in (PHASE_SHOWDOWN, PHASE_HAND_OVER):
            raise RuntimeError(f"cannot settle in phase {self.phase}")
        contested = self.engine.contested()
        if len(contested) > 1 and len(self.engine.board) < 5:
            raise RuntimeError("board incomplete: reveal remaining streets "
                               "via the mental deal before settling")
        # Audit #65: a contested seat still holding DUMMY cards would be
        # scored on them -- a wrong winner, invisible to chip conservation
        # because a wrong payout still sums correctly. Refuse instead.
        unverified = sorted(p.idx for p in contested if p.idx not in self._real_holes)
        if len(contested) > 1 and unverified:
            raise UnverifiedHolesError(
                f"cannot settle hand {self.hand_no}: contested seat(s) "
                f"{unverified} have no audit-verified hole cards")
        raw = self.engine.settle(runs=1, force_tabled=force_tabled)
        total = sum(self.stacks)
        if total != self.start_total:
            raise ChipConservationError(
                f"chip conservation failed: hand {self.hand_no} settled to "
                f"{total} chips but was dealt with {self.start_total}")
        self.result = _normalize_result(raw)
        self.phase = PHASE_SETTLED
        return self.result

    # ------------------------------------------------------------- sync

    def state_digest(self) -> str:
        """Hash of all betting-relevant state. Replicas MUST agree after
        every applied action; hole cards are excluded (they differ across
        replicas until the audit)."""
        e = self.engine
        parts = [
            f"hand={self.hand_no}", f"seq={self.next_seq}",
            f"phase={self.phase}", f"street={e.street}",
            f"actor={e.actor}", f"button={e.button}",
            f"sb_seat={e.sb_seat}", f"bb_seat={e.bb_seat}",
            f"cur={e.current_bet}", f"minr={e.min_raise}",
            f"nraises={e.raises_this_street}",
            f"need={sorted(e.need_to_act)}", f"noraise={sorted(e.no_raise)}",
            f"board={[(c.v, c.s) for c in e.board]}",
        ]
        for p in e.players:
            parts.append(
                f"p{p.idx}=({p.stack},{p.bet},{p.total_live},{p.total_dead},"
                f"{int(p.folded)},{int(p.all_in)},{int(p.in_seat)})")
        if self.result is not None:
            parts.append(f"result={self.result!r}")
        return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _normalize_result(raw: dict) -> dict:
    """settle()'s result contains sets and Card objects; canonicalize to
    sorted lists / (v, s) tuples so replicas can compare byte-for-byte."""
    def norm(v):
        if isinstance(v, set):
            return sorted(norm(x) for x in v)
        if isinstance(v, dict):
            return {k: norm(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
        if isinstance(v, (list, tuple)):
            return [norm(x) for x in v]
        if isinstance(v, Card):
            return (v.v, v.s)
        return v
    return norm(raw)


__all__ = ["ReplicaTable", "ChipConservationError", "PHASE_BETTING",
           "PHASE_STREET_OVER", "PHASE_SHOWDOWN", "PHASE_HAND_OVER",
           "PHASE_SETTLED"]
