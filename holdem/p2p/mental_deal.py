"""MentalDeal coordinator — Phases A+B (L5 step 2).

The heart of L5: a peer-symmetric state machine that runs a full
mental-poker hand (DKG -> shuffle chain -> deal -> audit) over the crypto
stack, with NO host and NO network. Each seat runs its own MentalDeal
instance; every instance consumes the same broadcast messages and reaches
the same public state. Transport is decoupled — methods take and return
message dicts (``{"type": ..., ...}``), so an n-instance in-process
simulation drives and tests the whole protocol with no sockets.

Built phase by phase. Implemented so far:
  Phase A -- distributed key ceremony (DKG) with per-share PoP.
  Phase B -- shuffle chain from the trivial deck.
Phases C (selective threshold deal) and D (post-hand audit) land next.

Design commitments (from the settled L5 decisions)
--------------------------------------------------
- **Peer-symmetric.** No seat coordinates. Canonical rules every seat
  computes identically drive turn-taking (the shuffle order is the sorted
  seat list) and tallying.
- **Transport-agnostic.** ``start()`` returns the outbound messages this
  seat should broadcast; ``handle(msg)`` consumes one inbound broadcast
  and returns any outbound messages it triggers. The caller moves bytes.
  NOTE: the shuffle chain assumes a seat receives an echo of its OWN
  broadcast (the in-process harness delivers every message to every seat,
  including the sender). A real transport must either loop back a sender's
  own messages or the wiring layer must self-deliver.
- **Deterministic key shares.** x_share = HKDF(master_secret,
  session|hand|seat) -- a crashed/reopened app regenerates the identical
  share (crash-survival decision). The master secret never leaves the
  process.
- **Detection-only by default.** Per the settled decision, the v1 default
  attaches NO shuffle proof to a deck round; a cheating shuffle is caught
  by the Phase D post-hand audit. The opt-in prevention layer (attaching
  and verifying a shadow-deck shuffle_proof per round) wires in on top of
  this and is added next.
- **Fail-closed with attribution.** A protocol violation aborts the hand
  and names the offending seat; there is no skip-and-continue.

Message types
-------------
- ``key_announce {seat, X_hex, pop_hex}`` (Phase A) -- each seat's public
  key share and its proof-of-possession.
- ``deck_round {round, seat, deck}`` (Phase B) -- the shuffled deck after
  round ``round`` (1-based), produced by ``seat``; ``deck`` is a list of
  [c0_hex, c1_hex] ciphertext pairs. Round 0 is the trivial deck, held
  implicitly and never transmitted.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from holdem.p2p import ristretto as R
from holdem.p2p import keygen_pop
from holdem.p2p import elgamal as eg
from holdem.p2p import shuffle_mp
from holdem.p2p.ristretto import Point, Scalar
from holdem.p2p.elgamal import Ciphertext


class Phase(Enum):
    KEYGEN = "keygen"
    SHUFFLE = "shuffle"
    DEAL = "deal"              # Phase C (not yet implemented)
    AUDIT = "audit"            # Phase D
    DONE = "done"
    ABORTED = "aborted"


def derive_share(master_secret: bytes, session_id: str, hand_no: int,
                 seat: int) -> Scalar:
    """Deterministic secret key share x_i = HKDF(master, session|hand|seat).

    HKDF-Expand (RFC 5869) over SHA-256 with an info label binding the
    ceremony context, then reduced into the Ristretto255 scalar field.
    Deterministic: the same inputs always yield the same share, so a
    rejoining seat regenerates exactly its share. The master secret is a
    local device secret and never leaves the process.
    """
    info = f"poker.share.v1|{session_id}|{hand_no}|{seat}".encode()
    t = b""
    okm = b""
    counter = 1
    while len(okm) < 64:
        t = hmac.new(master_secret, t + info + bytes([counter]),
                     hashlib.sha256).digest()
        okm += t
        counter += 1
    return R.scalar_reduce(okm[:64])


def _pop_ctx(session_id: str, hand_no: int, seat: int) -> bytes:
    """The PoP context binding: session, hand, and the announcing seat."""
    return f"poker.dkg.v1|{session_id}|{hand_no}|{seat}".encode()


@dataclass
class MentalDeal:
    """One seat's view of a mental-poker hand. Peer-symmetric state machine.

    Construct one per seat with the shared public parameters and this
    seat's private inputs, call ``start()`` to get the messages to
    broadcast, and feed every inbound broadcast (including echoes of your
    own) to ``handle()``.
    """
    session_id: str
    hand_no: int
    seat: int                       # this instance's seat index
    seats_in: List[int]             # all seat indices in the hand (sorted)
    button: int
    master_secret: bytes            # local device secret (never sent)

    # --- internal state ---
    phase: Phase = Phase.KEYGEN
    _x_share: Optional[Scalar] = None                     # my secret (local only)
    _pubkeys: Dict[int, Point] = field(default_factory=dict)   # seat -> X_i
    _joint_pk: Optional[Point] = None
    _deck: Optional[List[Ciphertext]] = None              # current accepted deck
    _shuffle_round: int = 0                               # rounds accepted so far
    abort_reason: Optional[str] = None
    bad_seat: Optional[int] = None
    _announced: bool = False

    def __post_init__(self):
        self.seats_in = sorted(self.seats_in)
        if self.seat not in self.seats_in:
            raise ValueError(f"seat {self.seat} not in seats_in {self.seats_in}")

    # ---------------------------------------------------------------- helpers

    def _abort(self, reason: str, bad_seat: Optional[int] = None) -> List[dict]:
        self.phase = Phase.ABORTED
        self.abort_reason = reason
        self.bad_seat = bad_seat
        return []

    @property
    def joint_pk(self) -> Optional[Point]:
        return self._joint_pk

    @property
    def deck(self) -> Optional[List[Ciphertext]]:
        """The current accepted deck (trivial deck, then each shuffle)."""
        return self._deck

    def is_done_with_keygen(self) -> bool:
        return self._joint_pk is not None

    def is_shuffle_complete(self) -> bool:
        return (self._shuffle_round == len(self.seats_in)
                and self.phase in (Phase.DEAL, Phase.AUDIT, Phase.DONE))

    # ---------------------------------------------------------------- dispatch

    def start(self) -> List[dict]:
        """Begin the hand: derive this seat's share and announce it.

        Returns the single ``key_announce`` message this seat broadcasts.
        Idempotent — calling twice does not re-announce.
        """
        if self.phase != Phase.KEYGEN or self._announced:
            return []
        self._x_share = derive_share(self.master_secret, self.session_id,
                                     self.hand_no, self.seat)
        X = R.mul_base(self._x_share)
        pop = keygen_pop.prove(self._x_share,
                               _pop_ctx(self.session_id, self.hand_no, self.seat))
        self._announced = True
        self._pubkeys[self.seat] = X            # record our own share
        return [{
            "type": "key_announce",
            "seat": self.seat,
            "X_hex": bytes(X).hex(),
            "pop_hex": pop.hex(),
        }]

    def handle(self, msg: dict) -> List[dict]:
        """Consume one inbound broadcast; return any outbound messages."""
        if self.phase == Phase.ABORTED:
            return []
        mtype = msg.get("type")
        if mtype == "key_announce":
            return self._on_key_announce(msg)
        if mtype == "deck_round":
            return self._on_deck_round(msg)
        return []

    # ---------------------------------------------------------------- Phase A

    def _on_key_announce(self, msg: dict) -> List[dict]:
        if self.phase != Phase.KEYGEN:
            return []
        seat = msg["seat"]
        if seat not in self.seats_in:
            return self._abort(f"key_announce from unknown seat {seat}", seat)

        try:
            X = R.point_from_bytes(bytes.fromhex(msg["X_hex"]))
            pop = bytes.fromhex(msg["pop_hex"])
        except (ValueError, KeyError):
            return self._abort(f"malformed key_announce from seat {seat}", seat)

        if not keygen_pop.verify(X, pop, _pop_ctx(self.session_id, self.hand_no, seat)):
            return self._abort(f"seat {seat} failed key-share proof-of-possession",
                               seat)

        if seat in self._pubkeys and bytes(self._pubkeys[seat]) != bytes(X):
            return self._abort(f"seat {seat} announced conflicting key shares", seat)

        self._pubkeys[seat] = X

        if all(s in self._pubkeys for s in self.seats_in):
            return self._finish_keygen()
        return []

    def _finish_keygen(self) -> List[dict]:
        # deterministic PK = sum of shares in canonical seat order
        ordered = [self._pubkeys[s] for s in self.seats_in]
        self._joint_pk = eg.joint_public_key(ordered)
        self.phase = Phase.SHUFFLE
        # the shuffle chain starts from the inspection-verifiable trivial deck
        self._deck = eg.make_trivial_deck()
        self._shuffle_round = 0
        # if this seat is the first shuffler, kick off round 1
        return self._maybe_emit_shuffle()

    # ---------------------------------------------------------------- Phase B

    def _expected_shuffler(self, round_no: int) -> Optional[int]:
        """Which seat shuffles round ``round_no`` (1-based), or None if the
        chain is complete."""
        if 1 <= round_no <= len(self.seats_in):
            return self.seats_in[round_no - 1]
        return None

    def _maybe_emit_shuffle(self) -> List[dict]:
        """If it is this seat's turn to shuffle the next round, produce and
        broadcast the shuffled deck. Changes NO local state -- the deck is
        applied uniformly by _on_deck_round when the echo arrives, so every
        seat (including this one) advances identically.
        """
        next_round = self._shuffle_round + 1
        if self._expected_shuffler(next_round) != self.seat:
            return []
        deck, _wit = shuffle_mp.shuffle_deck(self._joint_pk, self._deck)
        return [{
            "type": "deck_round",
            "round": next_round,
            "seat": self.seat,
            "deck": [ct.to_hex() for ct in deck],
        }]

    def _on_deck_round(self, msg: dict) -> List[dict]:
        if self.phase != Phase.SHUFFLE:
            return []                       # not shuffling (yet / anymore)

        try:
            round_no = int(msg["round"])
            seat = int(msg["seat"])
            raw = msg["deck"]
        except (KeyError, ValueError, TypeError):
            return self._abort("malformed deck_round", None)

        # must be exactly the next round in sequence
        if round_no != self._shuffle_round + 1:
            return []                       # duplicate/echo/out-of-order: ignore

        # must come from the seat whose turn it is
        expected = self._expected_shuffler(round_no)
        if seat != expected:
            return self._abort(
                f"seat {seat} shuffled out of turn (round {round_no} "
                f"belongs to seat {expected})", seat)

        # parse and structurally validate the deck
        try:
            deck = [Ciphertext.from_hex(pair) for pair in raw]
        except (ValueError, TypeError):
            return self._abort(f"seat {seat} sent an unparseable deck", seat)
        if len(deck) != 52:
            return self._abort(
                f"seat {seat} sent a deck of {len(deck)} cards (expected 52)", seat)
        # a genuine shuffle re-encrypts, so no ciphertext may be trivial
        # (C0 == identity would be an unshuffled / smuggled card)
        if any(bytes(ct.c0) == bytes(R.IDENTITY) for ct in deck):
            return self._abort(
                f"seat {seat} sent a deck containing a trivial ciphertext", seat)

        # (prevention mode would verify a shadow-deck shuffle_proof here,
        #  against self._deck as the previous deck. Detection-only default
        #  relies on the Phase D audit instead.)

        # accept
        self._deck = deck
        self._shuffle_round = round_no

        if round_no == len(self.seats_in):
            # shuffle chain complete; Phase C (deal) begins here once built
            self.phase = Phase.DEAL
            return []
        return self._maybe_emit_shuffle()


__all__ = ["MentalDeal", "Phase", "derive_share"]
