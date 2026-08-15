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
  by the Phase D post-hand audit. Setting ``prevention=True`` opts into
  the Bayer-Groth prevention layer described below. The default is
  unchanged and byte-identical to the pre-prevention protocol.
- **Fail-closed, with attribution wherever the evidence identifies a
  seat.** A protocol violation aborts the hand; there is no
  skip-and-continue. Every violation attributable to a seat from the
  message that caused it names that seat: a bad PoP, an out-of-turn
  shuffle, a bad decryption proof, an invalid prevention proof.

  One case does not name a seat. In detection-only, a shuffler that
  substitutes a card is caught by the Phase D multiset check, but that
  check runs over the FINAL deck and says only that the chain was
  corrupted somewhere -- ``bad_seat`` is None. Pinning the round would
  mean auditing each intermediate deck, and since the audit shares are
  computed against a specific deck's C0 values, that needs every seat to
  open every round -- a new message exchange, not a local computation.
  ``deck_audit.first_corrupt_round`` implements the analysis; nothing
  invokes it, because the exchange that would feed it does not exist.

  DESIGN DECISION PENDING: adding that exchange is a wire-protocol change
  costing n x the audit on the failure path, and it is not required for
  correctness -- the hand already voids fail-closed and no cards or chips
  are at risk. Prevention mode does not have this gap at all: a corrupt
  shuffle is rejected at the round that produced it, naming the shuffler,
  before the deck is ever accepted. Tested both ways in
  tests/test_deck_audit_soundness.py.

Prevention mode (opt-in)
------------------------
With ``prevention=True`` every shuffler attaches a Bayer-Groth shuffle
argument (``bg_shuffle``) to its ``deck_round``, and every peer verifies
it against the previous deck BEFORE accepting the new one. A missing,
undecodable, or invalid proof all take the same abort path, attributed to
the sending shuffler, each with its own reason string.

Prevention is a TABLE-WIDE setting. This layer performs no negotiation
and assumes the session layer established a uniform mode; a mixed-mode
table voids the hand rather than silently degrading.

The commitment key is NUMS-derived from a fixed public seed, so every
peer reconstructs an identical key with nothing on the wire and the
Scytl/Swiss-Post trapdoor class stays structurally impossible (see
pedersen.py). ``shuffle_proof.py``'s cut-and-choose construction is
untouched by this path and remains available standalone.

Message types
-------------
- ``key_announce {seat, X_hex, pop_hex}`` (Phase A) -- each seat's public
  key share and its proof-of-possession.
- ``deck_round {round, seat, deck}`` (Phase B) -- the shuffled deck after
  round ``round`` (1-based), produced by ``seat``; ``deck`` is a list of
  [c0_hex, c1_hex] ciphertext pairs. Round 0 is the trivial deck, held
  implicitly and never transmitted. In prevention mode the message
  carries one additional key, ``proof`` (see bg_wire.py); in
  detection-only mode the key is absent and an unsolicited one is
  ignored, so a prevention-enabled peer cannot disrupt a
  detection-only table.
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
from holdem.p2p import dleq
from holdem.p2p import deal_map as dmap
from holdem.p2p import deck_audit
from holdem.p2p import bg_shuffle
from holdem.p2p import bg_wire
from holdem.p2p.pedersen import CommitmentKey
from holdem.p2p.ristretto import Point, Scalar
from holdem.p2p.elgamal import Ciphertext


# ---------------------------------------------------------------- prevention
# Bayer-Groth shuffle-argument parameters for a 52-card deck. The proof
# arranges the deck as an m x n matrix, so m * n must equal 52; 4 x 13 is
# the benchmarked configuration (docs/BG_SHUFFLE_BENCHMARK.md).
BG_M = 4
BG_N = 13

# Public NUMS seed for the Pedersen commitment key. Every peer derives an
# identical key from this constant, so no key material is transmitted and
# nobody can hold a commitment trapdoor. Changing it is a wire break.
BG_CK_SEED = b"poker.mentaldeal.bg.ck.v1"

# Upper bound on messages held because they arrived before this seat could
# act on them. A nine-seat hand legitimately has at most ~189 deal messages
# in flight, so honest reordering never approaches this; it exists so a peer
# replaying junk cannot grow the buffer without limit.
MAX_HELD = 1024

_BG_CK: Optional[CommitmentKey] = None


def bg_commitment_key() -> CommitmentKey:
    """The shared prevention-mode commitment key (derived once, cached).

    Built lazily so a detection-only table never pays the derivation.
    """
    global _BG_CK
    if _BG_CK is None:
        _BG_CK = CommitmentKey.generate(BG_N, seed=BG_CK_SEED)
    return _BG_CK


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


# board slots revealed per street (indices into board_positions() output)
_STREET_SLOTS = {"flop": (0, 1, 2), "turn": (3,), "river": (4,)}


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
    prevention: bool = False        # opt-in Bayer-Groth shuffle proofs

    # --- internal state ---
    phase: Phase = Phase.KEYGEN
    _x_share: Optional[Scalar] = None                     # my secret (local only)
    _pubkeys: Dict[int, Point] = field(default_factory=dict)   # seat -> X_i
    _joint_pk: Optional[Point] = None
    _deck: Optional[List[Ciphertext]] = None              # current accepted deck
    _shuffle_round: int = 0                               # rounds accepted so far
    #: Shuffle proofs this instance has VERIFIED, incremented only where
    #: bg_shuffle.verify actually returned true. Evidence, not a label: it
    #: is the one value that distinguishes a table running Bayer-Groth from
    #: one that merely says it is, and it stays zero in detection-only
    #: because nothing verifies anything there.
    _proofs_verified: int = 0
    # Phase C (deal) state
    _deal_map: Optional[List[dmap.Destination]] = None
    _hole_pos: Dict[int, List[int]] = field(default_factory=dict)   # seat -> [pos,pos]
    _board_pos: List[int] = field(default_factory=list)   # deck pos by board slot 0..4
    _shares: Dict[int, Dict[int, Point]] = field(default_factory=dict)  # pos->seat->D
    _hole: List[Optional[str]] = field(default_factory=lambda: [None, None])
    _board: List[Optional[str]] = field(default_factory=lambda: [None] * 5)
    _revealed_streets: set = field(default_factory=set)
    # Phase D (audit) state
    _round_decks: List[List[Ciphertext]] = field(default_factory=list)  # per-round history
    _audit_shares: Dict[int, list] = field(default_factory=dict)   # seat -> PositionShares
    _audit_report: Optional["deck_audit.AuditReport"] = None
    _audit_opened: bool = False
    abort_reason: Optional[str] = None
    bad_seat: Optional[int] = None
    _announced: bool = False
    # Messages that arrived before this seat could act on them, replayed
    # as the state advances. The transport does not guarantee ordering.
    _held: List[dict] = field(default_factory=list)

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

    @property
    def hole_cards(self) -> List[Optional[str]]:
        """This seat's two hole cards (labels), filled as shares arrive."""
        return list(self._hole)

    @property
    def board(self) -> List[Optional[str]]:
        """The board (5 slots), filled street by street as revealed."""
        return list(self._board)

    def hole_complete(self) -> bool:
        return all(c is not None for c in self._hole)

    def board_complete(self) -> bool:
        return all(c is not None for c in self._board)

    @property
    def audit_report(self):
        """The AuditReport once the post-hand audit has run, else None."""
        return self._audit_report

    @property
    def round_decks(self) -> List[List[Ciphertext]]:
        """Each accepted shuffle round's deck, in order (chain-audit source)."""
        return list(self._round_decks)

    def is_done(self) -> bool:
        return self.phase == Phase.DONE

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
        """Consume one inbound broadcast; return any outbound messages.

        A message that arrives before this seat can act on it is held and
        replayed, not dropped. The transport is explicitly unordered (see
        the module docstring), so a deck round can overtake the key
        announcement it depends on, or a deal share can overtake the
        shuffle. Dropping those silently stalls the seat forever: nothing
        in the protocol retransmits.
        """
        if self.phase == Phase.ABORTED:
            return []
        if self._premature(msg):
            self._hold(msg)
            return []
        out = self._dispatch(msg)
        out.extend(self._drain_held())
        return out

    def _dispatch(self, msg: dict) -> List[dict]:
        mtype = msg.get("type")
        if mtype == "key_announce":
            return self._on_key_announce(msg)
        if mtype == "deck_round":
            return self._on_deck_round(msg)
        if mtype == "deal_share":
            return self._on_deal_share(msg)
        if mtype == "audit_open":
            return self._on_audit_open(msg)
        return []

    def _premature(self, msg: dict) -> bool:
        """True if this message is valid but cannot be acted on YET.

        Distinct from stale (already applied -- drop) and malformed
        (abort). Only "too early" is held, so a duplicate or a bogus
        message still takes its normal path rather than accumulating.
        """
        mtype = msg.get("type")
        if mtype == "deck_round":
            if self.phase == Phase.KEYGEN:
                return True                 # joint key not formed yet
            if self.phase != Phase.SHUFFLE:
                return False                # chain finished: stale, not early
            try:
                round_no = int(msg["round"])
            except (KeyError, ValueError, TypeError):
                return False                # malformed: let _dispatch abort
            return round_no > self._shuffle_round + 1
        if mtype in ("deal_share", "audit_open"):
            return self.phase in (Phase.KEYGEN, Phase.SHUFFLE)
        return False

    def _hold(self, msg: dict) -> None:
        """Retain an early message, bounded.

        The legitimate in-flight total for a hand is n key announces, n
        deck rounds, n*2n deal shares and n audit opens -- 189 at nine
        seats. MAX_HELD is far above that, so honest reordering never
        reaches it, while a peer replaying junk cannot grow this without
        limit.
        """
        if len(self._held) >= MAX_HELD:
            self._abort(
                f"more than {MAX_HELD} out-of-order messages held; "
                f"refusing to buffer further", None)
            return
        self._held.append(dict(msg))

    def _drain_held(self) -> List[dict]:
        """Replay held messages that the new state has made actionable.

        Loops because applying one held message can unblock another (round
        2 arriving before round 1 unblocks once round 1 lands). Terminates
        because every pass either consumes at least one message or stops.
        """
        out: List[dict] = []
        while self._held and self.phase != Phase.ABORTED:
            ready, self._held = self._held, []
            consumed = False
            for held in ready:
                if self.phase == Phase.ABORTED:
                    break
                if self._premature(held):
                    self._held.append(held)
                else:
                    out.extend(self._dispatch(held))
                    consumed = True
            if not consumed:
                break
        if self.phase == Phase.ABORTED:
            self._held.clear()
        return out

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
        joint = eg.joint_public_key(ordered)
        # Defence in depth. keygen_pop.verify already rejects an identity
        # share, so reaching here should be impossible -- but under an
        # identity joint key ElGamal degenerates to C1 = M and the deck is
        # public, so this is worth failing closed on rather than trusting an
        # upstream check. Without it the failure would surface as an uncaught
        # ValueError from the first re-encryption, mid-protocol.
        if bytes(joint) == bytes(R.IDENTITY):
            return self._abort(
                "key ceremony produced a degenerate (identity) joint key",
                None)
        self._joint_pk = joint
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

    def _bg_ctx(self, round_no: int, seat: int) -> bytes:
        """Statement binding for a prevention proof.

        Binds session, hand, shuffle round, shuffling seat, and the
        commitment-key identity, so a proof is valid for exactly one
        (session, hand, round, seat, key) tuple and replaying it anywhere
        else yields a different statement hash and a clean verify-False.

        Fields are length-prefixed rather than delimiter-joined, which
        costs nothing and removes a footgun that turned out to be real
        one layer down. session_id was itself a "|"-joined string, and
        THAT join was ambiguous: ["a|b","c"] and ["a","b|c"] produced the
        same id, so two structurally different tables shared a DKG domain.
        This layer was never the problem -- one variable-shape field
        first, then separator-free integers -- but it is the discipline
        session._deal_session_id now also follows, where the id is a
        digest of a canonically-encoded context that binds the deal
        policy.

        The commitment key is additionally bound inside the proof itself --
        bg_shuffle._statement_context hashes ck.H and every ck.Gs. Naming
        the seed here too costs nothing and turns a key mismatch into an
        attributable abort instead of a bare verification failure.
        """
        parts = [
            b"poker.mentaldeal.bg.v1",
            self.session_id.encode(),
            str(self.hand_no).encode(),
            str(round_no).encode(),
            str(seat).encode(),
            BG_CK_SEED,
        ]
        out = bytearray()
        for part in parts:
            out += len(part).to_bytes(4, "big")
            out += part
        return bytes(out)

    def _maybe_emit_shuffle(self) -> List[dict]:
        """If it is this seat's turn to shuffle the next round, produce and
        broadcast the shuffled deck. Changes NO local state -- the deck is
        applied uniformly by _on_deck_round when the echo arrives, so every
        seat (including this one) advances identically.

        In prevention mode the shuffle witness (which is never transmitted)
        is consumed here to build the Bayer-Groth proof and then discarded
        with the local frame, so the no-local-state property holds either
        way.
        """
        next_round = self._shuffle_round + 1
        if self._expected_shuffler(next_round) != self.seat:
            return []
        deck, wit = shuffle_mp.shuffle_deck(self._joint_pk, self._deck)
        msg = {
            "type": "deck_round",
            "round": next_round,
            "seat": self.seat,
            "deck": [ct.to_hex() for ct in deck],
        }
        if self.prevention:
            proof = bg_shuffle.prove(
                self._joint_pk, bg_commitment_key(), self._deck, deck,
                wit.perm, wit.scalars, BG_M, BG_N,
                self._bg_ctx(next_round, self.seat))
            msg["proof"] = bg_wire.encode(proof)
        return [msg]

    def _prevention_failure(self, msg: dict, deck: List[Ciphertext],
                            round_no: int, seat: int) -> Optional[str]:
        """Check a round's prevention proof; None if it is acceptable.

        Missing, undecodable, and invalid proofs are all unacceptable and
        all reach the same abort, attributed to ``seat`` -- but each
        returns its own reason so logs and tests can tell them apart.
        """
        raw = msg.get("proof")
        if raw is None:
            return (f"seat {seat} omitted the required shuffle proof "
                    f"for round {round_no}")
        try:
            proof = bg_wire.decode(raw, BG_M, BG_N)
        except ValueError as exc:
            return (f"seat {seat} sent an undecodable shuffle proof "
                    f"for round {round_no}: {exc}")
        if not bg_shuffle.verify(
                self._joint_pk, bg_commitment_key(), self._deck, deck,
                BG_M, BG_N, self._bg_ctx(round_no, seat), proof):
            return (f"seat {seat} sent an invalid shuffle proof "
                    f"for round {round_no}")
        return None

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

        # Prevention mode: the round is only acceptable if it carries a
        # valid Bayer-Groth proof against self._deck (the previous deck).
        # Detection-only relies on the Phase D audit instead, and ignores
        # any proof that happens to be attached.
        if self.prevention:
            failure = self._prevention_failure(msg, deck, round_no, seat)
            if failure is not None:
                return self._abort(failure, seat)
            # Counted only past the failure check, so this records proofs
            # that verified rather than proofs that arrived.
            self._proofs_verified += 1

        # accept
        self._deck = deck
        self._shuffle_round = round_no
        self._round_decks.append(deck)          # retain history for chain attribution

        if round_no == len(self.seats_in):
            # shuffle chain complete; begin the deal (hole cards)
            return self._enter_deal()
        return self._maybe_emit_shuffle()

    # ---------------------------------------------------------------- Phase C

    def _valid_positions(self) -> set:
        """Deck positions that are actually dealt (all holes + the board)."""
        positions = set(self._board_pos)
        for plist in self._hole_pos.values():
            positions.update(plist)
        return positions

    def _make_share_msg(self, pos: int) -> dict:
        """Compute this seat's DLEQ-proven decryption share for ``pos``,
        record it locally, and return the broadcast message."""
        ct = self._deck[pos]
        D = eg.partial_decrypt(ct, self._x_share)
        proof = dleq.prove(self._x_share, ct.c0)
        self._shares.setdefault(pos, {})[self.seat] = D
        return {
            "type": "deal_share",
            "position": pos,
            "seat_from": self.seat,
            "D_hex": bytes(D).hex(),
            "dleq_hex": proof.hex(),
        }

    def _enter_deal(self) -> List[dict]:
        """Begin the deal: broadcast hole-card decryption shares.

        Privacy is cryptographic, not delivery-based: for a hole card owned
        by seat t, every OTHER seat broadcasts its share, and t's withheld
        share masks the plaintext from everyone else -- so only t can
        combine and recover the card. This seat therefore broadcasts a share
        for every hole position it does NOT own, and merely records (never
        sends) its own share for its own hole positions. Board cards stay
        undealt until reveal_street() (else the board would leak before
        betting).
        """
        self.phase = Phase.DEAL
        self._deal_map = dmap.deal_map(self.button, self.seats_in)
        self._hole_pos = dmap.hole_positions(self.button, self.seats_in)
        self._board_pos = dmap.board_positions(self.button, self.seats_in)

        # record (do not send) my share for my own hole positions
        for pos in self._hole_pos.get(self.seat, []):
            D = eg.partial_decrypt(self._deck[pos], self._x_share)
            self._shares.setdefault(pos, {})[self.seat] = D

        # broadcast my share for every other seat's hole positions
        msgs: List[dict] = []
        for owner, positions in self._hole_pos.items():
            if owner == self.seat:
                continue
            for pos in positions:
                msgs.append(self._make_share_msg(pos))
        return msgs

    def reveal_street(self, street: str) -> List[dict]:
        """Broadcast this seat's board shares for a street's slots.

        Called by the wiring layer once the preceding betting round closes,
        so the board is revealed progressively rather than all at once.
        ``street`` is "flop" (slots 0-2), "turn" (slot 3), or "river"
        (slot 4). Idempotent per street.
        """
        if self.phase not in (Phase.DEAL, Phase.AUDIT):
            return []
        if street in self._revealed_streets:
            return []
        slots = _STREET_SLOTS.get(street)
        if slots is None:
            raise ValueError(f"unknown street {street!r}")
        self._revealed_streets.add(street)
        msgs = [self._make_share_msg(self._board_pos[slot]) for slot in slots]
        for slot in slots:                       # handles a 1-seat edge case
            self._try_complete(self._board_pos[slot])
        return msgs

    def _on_deal_share(self, msg: dict) -> List[dict]:
        if self.phase not in (Phase.DEAL, Phase.AUDIT):
            return []
        try:
            pos = int(msg["position"])
            seat_from = int(msg["seat_from"])
            D = R.point_from_bytes(bytes.fromhex(msg["D_hex"]))
            proof = bytes.fromhex(msg["dleq_hex"])
        except (KeyError, ValueError, TypeError):
            return self._abort("malformed deal_share", None)

        if seat_from not in self.seats_in:
            return self._abort(f"deal_share from unknown seat {seat_from}", seat_from)
        if pos not in self._valid_positions():
            return self._abort(f"deal_share for undealt position {pos}", seat_from)

        # DLEQ: proves D = x_{seat_from} * C0, tied to that seat's pubkey
        if not dleq.verify(self._pubkeys[seat_from], D, self._deck[pos].c0, proof):
            return self._abort(
                f"seat {seat_from} sent a bad decryption proof at position {pos}",
                seat_from)

        self._shares.setdefault(pos, {})[seat_from] = D
        self._try_complete(pos)
        return []

    def _try_complete(self, pos: int) -> None:
        """Combine a position's shares into a card -- but only for positions
        this seat is entitled to see (the board, or its OWN holes). Another
        seat's hole is never combined even if all shares happened to arrive.
        """
        n = len(self.seats_in)
        have = self._shares.get(pos, {})
        if len(have) < n:
            return
        shares = [have[s] for s in self.seats_in]

        if pos in self._board_pos:
            slot = self._board_pos.index(pos)
            if self._board[slot] is None:
                self._board[slot] = eg.point_to_card(
                    eg.combine(self._deck[pos], shares))
        elif pos in self._hole_pos.get(self.seat, []):
            ordinal = self._hole_pos[self.seat].index(pos)
            if self._hole[ordinal] is None:
                self._hole[ordinal] = eg.point_to_card(
                    eg.combine(self._deck[pos], shares))
        # else: another seat's hole -> never combined (privacy)

    # ---------------------------------------------------------------- Phase D

    def open_audit(self) -> List[dict]:
        """Open the post-hand audit: broadcast DLEQ-proven decryption shares
        for ALL 52 deck positions.

        Called by the wiring layer at hand end (showdown). Every seat's full
        opening lets everyone verify the final deck was an honest permutation
        of the 52 canonical cards; a corrupt deck fails the multiset check
        with certainty, and a lying decryptor is pinned by seat. Accepted
        consequence: mucked and burned cards become public here. Idempotent.
        """
        if self.phase not in (Phase.DEAL, Phase.AUDIT) or self._audit_opened:
            return []
        self.phase = Phase.AUDIT
        self._audit_opened = True
        shares = deck_audit.make_shares(self._deck, self._x_share)
        self._audit_shares[self.seat] = shares
        msg = {
            "type": "audit_open",
            "seat": self.seat,
            "shares": [[bytes(ps.share).hex(), ps.proof.hex()] for ps in shares],
        }
        self._maybe_run_audit()
        return [msg]

    def _on_audit_open(self, msg: dict) -> List[dict]:
        if self.phase not in (Phase.DEAL, Phase.AUDIT):
            return []
        try:
            seat = int(msg["seat"])
            raw = msg["shares"]
            shares = [
                deck_audit.PositionShare(
                    share=R.point_from_bytes(bytes.fromhex(pair[0])),
                    proof=bytes.fromhex(pair[1]),
                )
                for pair in raw
            ]
        except (KeyError, ValueError, TypeError, IndexError):
            return self._abort("malformed audit_open", None)

        if seat not in self.seats_in:
            return self._abort(f"audit_open from unknown seat {seat}", seat)

        self._audit_shares[seat] = shares
        self._maybe_run_audit()
        return []

    def _maybe_run_audit(self) -> None:
        """Once every seat has opened, run the full-deck audit and settle."""
        if any(s not in self._audit_shares for s in self.seats_in):
            return
        if self._audit_report is not None:
            return                              # already settled
        pubkeys = [self._pubkeys[s] for s in self.seats_in]
        shares_by_seat = [self._audit_shares[s] for s in self.seats_in]
        report = deck_audit.audit_deck(self._deck, pubkeys, shares_by_seat)
        self._audit_report = report
        if report.ok:
            self.phase = Phase.DONE
        else:
            # Void, naming a seat when the evidence identifies one. A lying
            # decryptor is pinned by its own failed DLEQ and appears in
            # bad_seats. A corrupt deck with no bad decryptor is a shuffler
            # cheat, but the multiset check only sees the final deck, so
            # blame is None -- see the module docstring for why attributing
            # it needs a new message exchange and why that is a pending
            # design decision rather than a correctness gap.
            blame = report.bad_seats[0] if report.bad_seats else None
            self._abort("; ".join(report.problems) or "audit failed", blame)


__all__ = ["MentalDeal", "Phase", "derive_share", "bg_commitment_key",
           "BG_M", "BG_N", "BG_CK_SEED"]
