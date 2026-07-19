"""Post-hand full-deck audit (mental-poker step 2 of the adopted plan).

At hand end, every seat partially decrypts ALL 52 positions of the final
shuffled deck and publishes each share with a DLEQ proof. Everyone then
combines the shares and checks that the recovered plaintexts are exactly
the canonical 52-card deck -- each card once, no strangers.

What this buys (covert security):
- A seat that submits a bogus decryption share is identified by name --
  its DLEQ fails against its own public key share.
- A shuffler that substituted, duplicated, or dropped a card is detected
  with certainty -- the multiset check fails -- and the hand is void.
- Refusal to publish shares is itself attributable (handled at the
  session layer via timeouts).

Detection, not prevention: a cheat is always CAUGHT (and the hand
voided), never stopped in advance. Prevention is step 3, the shadow-deck
cut-and-choose shuffle proof. The two compose.

Attribution of a corrupt deck to a specific SHUFFLER (not just "someone
cheated") uses the chain: every intermediate deck was broadcast, so on a
failed final audit the seats audit each round's output in order; the
first round whose deck fails the multiset check is the round whose
shuffler cheated (``first_corrupt_round``). Post-hand this leaks nothing
that matters: the audit already reveals every position's card, and a
finished hand's permutations have no remaining secrecy value.

Round 0 (the trivial deck) is NOT audited by decryption -- it is checked
by inspection via ``elgamal.verify_trivial_deck`` before any shuffle is
accepted, and a trivial ciphertext appearing in any LATER deck is itself
flagged as corrupt here.

Gameplay consequence, accepted by design: mucked and burned cards become
public at hand end.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from holdem.p2p import ristretto as R
from holdem.p2p import dleq
from holdem.p2p.ristretto import Point, Scalar
from holdem.p2p.elgamal import CARDS, Ciphertext, combine, point_to_card


@dataclass(frozen=True)
class PositionShare:
    """One seat's decryption share for one deck position, with its proof."""
    share: Point
    proof: bytes          # 64-byte DLEQ proof (dleq.prove)


def make_shares(deck: Sequence[Ciphertext], x_share: Scalar) -> List[PositionShare]:
    """A seat's audit contribution: a proven share for every position.

    Raises ValueError if the deck contains a trivial ciphertext
    (C0 = identity): a post-shuffle deck must consist of real
    ciphertexts, and DLEQ over an identity base is degenerate.
    """
    out: List[PositionShare] = []
    for i, ct in enumerate(deck):
        if bytes(ct.c0) == bytes(R.IDENTITY):
            raise ValueError(
                f"position {i}: trivial ciphertext in a post-shuffle deck")
        d = R.mul(x_share, ct.c0)
        out.append(PositionShare(share=d, proof=dleq.prove(x_share, ct.c0)))
    return out


@dataclass
class AuditReport:
    """Outcome of a full-deck audit."""
    ok: bool
    cards: List[Optional[str]]          # per-position recovered card (None = not a card)
    bad_seats: List[int] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)


def audit_deck(
    deck: Sequence[Ciphertext],
    seat_pubkeys: Sequence[Point],
    shares_by_seat: Sequence[Sequence[PositionShare]],
) -> AuditReport:
    """Verify every share's DLEQ, combine, and multiset-check the deck.

    ``seat_pubkeys[s]`` is seat s's public key share X_s; ``shares_by_seat
    [s]`` its ``make_shares`` output. Every seat holding a share of the
    joint key must contribute, or decryption is garbage by construction.
    """
    problems: List[str] = []
    bad_seats: List[int] = []
    n = len(deck)

    if n != len(CARDS):
        return AuditReport(ok=False, cards=[],
                           problems=[f"deck has {n} positions, expected {len(CARDS)}"])
    if len(shares_by_seat) != len(seat_pubkeys):
        return AuditReport(ok=False, cards=[],
                           problems=["one share list per seat pubkey required"])

    # structural checks + trivial-ciphertext scan
    for i, ct in enumerate(deck):
        if bytes(ct.c0) == bytes(R.IDENTITY):
            problems.append(f"position {i}: trivial ciphertext (invalid post-shuffle)")

    for s, shares in enumerate(shares_by_seat):
        if len(shares) != n:
            problems.append(f"seat {s}: {len(shares)} shares, expected {n}")
            bad_seats.append(s)

    if problems:
        return AuditReport(ok=False, cards=[], bad_seats=sorted(set(bad_seats)),
                           problems=problems)

    # DLEQ verification, per seat: the share for position i must be proven
    # against THIS seat's pubkey and THIS position's C0.
    for s, (X, shares) in enumerate(zip(seat_pubkeys, shares_by_seat)):
        for i, ps in enumerate(shares):
            if not dleq.verify(X, ps.share, deck[i].c0, ps.proof):
                bad_seats.append(s)
                problems.append(f"seat {s}: DLEQ failed at position {i}")
                break                      # one attribution per seat is enough

    # combine every position and map to cards
    cards: List[Optional[str]] = []
    for i, ct in enumerate(deck):
        shares_i = [shares_by_seat[s][i].share for s in range(len(seat_pubkeys))]
        m = combine(ct, shares_i)
        card = point_to_card(m)
        cards.append(card)
        if card is None:
            problems.append(f"position {i}: decrypts to a non-card point")

    # multiset check: exactly the canonical 52
    have = Counter(c for c in cards if c is not None)
    want = Counter(CARDS)
    if have != want:
        dupes = sorted(c for c, k in have.items() if k > want[c])
        missing = sorted(c for c in want if have[c] < want[c])
        if dupes:
            problems.append(f"duplicated cards: {', '.join(dupes)}")
        if missing:
            problems.append(f"missing cards: {', '.join(missing)}")

    bad_seats = sorted(set(bad_seats))
    return AuditReport(ok=not problems and not bad_seats,
                       cards=cards, bad_seats=bad_seats, problems=problems)


def first_corrupt_round(reports: Sequence[AuditReport]) -> Optional[int]:
    """Index of the first failing report in a chain audit, or None.

    ``reports[i]`` must be the audit of shuffle round i+1's OUTPUT deck
    (round 0, the trivial deck, is checked by inspection instead). If the
    final audit failed, auditing each round's broadcast output in order
    and taking the first failure attributes the corruption to that
    round's shuffler: their input deck audited clean, their output did not.
    """
    for i, rep in enumerate(reports):
        if not rep.ok:
            return i
    return None


__all__ = ["PositionShare", "AuditReport", "make_shares", "audit_deck",
           "first_corrupt_round"]
