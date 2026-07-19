"""MentalDeal coordinator — Phase A: distributed key ceremony (L5 step 2).

The heart of L5: a peer-symmetric state machine that runs a full
mental-poker hand (DKG -> shuffle chain -> deal -> audit) over the crypto
stack, with NO host and NO network. Each seat runs its own MentalDeal
instance; every instance consumes the same broadcast messages and reaches
the same public state. Transport is decoupled — methods take and return
message dicts (``{"type": ..., ...}``), so an n-instance in-process
simulation drives and tests the whole protocol with no sockets.

This module is built phase by phase. THIS commit is **Phase A only**:
the distributed key ceremony that establishes the joint encryption key
PK = sum_i X_i with a proof-of-possession per share (keygen_pop), closing
the rogue-key attack. Phases B (shuffle chain), C (deal), and D (audit)
land on this foundation next.

Design commitments (from the settled L5 decisions)
--------------------------------------------------
- **Peer-symmetric.** No seat coordinates. Canonical rules every seat
  computes identically drive turn-taking and tallying.
- **Transport-agnostic.** ``start()`` returns the outbound messages this
  seat should broadcast; ``handle(msg)`` consumes one inbound broadcast
  and returns any outbound messages it triggers. The caller moves bytes.
- **Deterministic key shares.** A seat's secret share is DERIVED, not
  randomly generated: ``x_share = HKDF(master_secret, session|hand|seat)``.
  So a crashed/reopened app regenerates the identical share instead of
  losing it (crash-survival decision). The master secret is a local
  device secret, never transmitted.
- **Fail-closed with attribution.** A bad PoP aborts the hand and names
  the offending seat; there is no "skip the bad share and continue."

Message types (Phase A)
-----------------------
- ``key_announce {seat, X_hex, pop_hex}`` — broadcast by every seat once,
  carrying its public key share and the PoP for it.

The ceremony completes for a seat when it has verified a valid
``key_announce`` from every seat in the hand (including its own echo);
it then computes PK deterministically and transitions to Phase B.
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
from holdem.p2p.ristretto import Point, Scalar


class Phase(Enum):
    KEYGEN = "keygen"
    SHUFFLE = "shuffle"        # Phase B (not yet implemented)
    DEAL = "deal"              # Phase C
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
    # HKDF-Expand to 64 bytes, then scalar_reduce (unbiased into the field).
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

    def is_done_with_keygen(self) -> bool:
        return self._joint_pk is not None

    # ---------------------------------------------------------------- Phase A

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
        # record our own share immediately (we still also process the echo)
        self._pubkeys[self.seat] = X
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
        # messages for later phases are ignored until those phases exist
        return []

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

        # verify the proof-of-possession, bound to this seat's context
        if not keygen_pop.verify(X, pop, _pop_ctx(self.session_id, self.hand_no, seat)):
            return self._abort(f"seat {seat} failed key-share proof-of-possession",
                               seat)

        # a seat announcing a different share than we already recorded is a fault
        if seat in self._pubkeys and bytes(self._pubkeys[seat]) != bytes(X):
            return self._abort(f"seat {seat} announced conflicting key shares", seat)

        self._pubkeys[seat] = X

        # ceremony complete once every seat's verified share is in
        if all(s in self._pubkeys for s in self.seats_in):
            return self._finish_keygen()
        return []

    def _finish_keygen(self) -> List[dict]:
        # deterministic PK = sum of shares in canonical seat order
        ordered = [self._pubkeys[s] for s in self.seats_in]
        self._joint_pk = eg.joint_public_key(ordered)
        self.phase = Phase.SHUFFLE
        return []          # Phase B kickoff lands when that phase is built


__all__ = ["MentalDeal", "Phase", "derive_share"]
