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
from holdem.p2p.elgamal import (CARDS, Ciphertext, combine, make_trivial_deck,
                                point_to_card, reencrypt)
from holdem.p2p.shuffle_mp import ShuffleWitness


# ------------------------------------------------------------------ binding
#
# The multiset check alone proves only that the audited deck is SOME legal
# deck. A shuffler can replace its input with a fresh encryption of the 52
# cards in an order it chose; that deck passes the multiset check while
# telling its author every hole card. Binding closes that: post-hand, each
# shuffler opens its witness (permutation + re-encryption scalars -- a
# finished hand's permutations have no secrecy value), and every round's
# broadcast output must be EXACTLY the re-encrypted permutation of its input,
# starting from the canonical trivial deck and ending at the audited deck.

@dataclass(frozen=True)
class ShuffleRound:
    """One accepted shuffle round: who shuffled, what they broadcast, and
    the witness they opened at audit time (None if they never opened)."""
    seat: int
    output: Sequence[Ciphertext]
    witness: Optional[ShuffleWitness]


@dataclass(frozen=True)
class ShuffleChain:
    """The full shuffle history of a hand, under the joint public key."""
    pk: Point
    rounds: Sequence[ShuffleRound]


def _ct_bytes(ct: Ciphertext) -> bytes:
    return bytes(ct.c0) + bytes(ct.c1)


def verify_shuffle_round(pk: Point, prev: Sequence[Ciphertext],
                         out: Sequence[Ciphertext],
                         witness: ShuffleWitness) -> Optional[str]:
    """None if ``out`` is exactly ``prev`` permuted by ``witness.perm`` and
    re-encrypted with ``witness.scalars``; otherwise a description."""
    n = len(prev)
    if len(out) != n:
        return f"output has {len(out)} positions, input {n}"
    perm, scalars = list(witness.perm), list(witness.scalars)
    if sorted(perm) != list(range(n)):
        return "witness permutation is not a permutation of the deck"
    if len(scalars) != n:
        return f"witness has {len(scalars)} scalars, expected {n}"
    for i, src in enumerate(perm):
        try:
            expect = reencrypt(pk, prev[src], scalars[i])
        except (ValueError, TypeError) as exc:
            return f"position {i}: witness scalar unusable ({exc})"
        if _ct_bytes(expect) != _ct_bytes(out[i]):
            return f"position {i} is not derived from input position {src}"
    return None


def verify_chain(chain: ShuffleChain,
                 final_deck: Sequence[Ciphertext]) -> tuple:
    """Check the chain from the trivial deck to ``final_deck``.

    Returns ``(problems, bad_seats)``. The FIRST round whose output does
    not follow from its input (or whose shuffler opened no witness) is
    attributed to that round's shuffler; later rounds are not judged,
    because their input is already untrusted.
    """
    if not chain.rounds:
        return ["shuffle chain is empty"], []
    prev: Sequence[Ciphertext] = make_trivial_deck()
    for r, rnd in enumerate(chain.rounds, start=1):
        if rnd.witness is None:
            return ([f"round {r} (seat {rnd.seat}): shuffle witness not opened"],
                    [rnd.seat])
        why = verify_shuffle_round(chain.pk, prev, rnd.output, rnd.witness)
        if why is not None:
            return ([f"round {r} (seat {rnd.seat}): output is not a shuffle "
                     f"of its input: {why}"], [rnd.seat])
        prev = rnd.output
    if (len(prev) != len(final_deck) or
            any(_ct_bytes(a) != _ct_bytes(b) for a, b in zip(prev, final_deck))):
        return (["audited deck is not the output of the shuffle chain"], [])
    return [], []


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
    *,
    chain: Optional[ShuffleChain],
) -> AuditReport:
    """Verify the deck descends from the played shuffle chain, verify every
    share's DLEQ, combine, and multiset-check the deck.

    ``seat_pubkeys[s]`` is seat s's public key share X_s; ``shares_by_seat
    [s]`` its ``make_shares`` output. Every seat holding a share of the
    joint key must contribute, or decryption is garbage by construction.

    ``chain`` is REQUIRED (keyword-only, no default): an audit that cannot
    show the deck came from the hand's shuffles proves nothing about it.
    Passing ``chain=None`` is allowed only to run the decryption checks
    for diagnostics; such a report is never ``ok``.
    """
    problems: List[str] = []
    bad_seats: List[int] = []
    n = len(deck)

    # Chain binding. Recorded up front but does not short-circuit: the
    # decryption checks below still run, so a lying decryptor is named
    # even when the deck is also unbound.
    if chain is None:
        problems.append("deck is not bound to a shuffle chain (no chain "
                        "evidence supplied)")
    else:
        chain_problems, chain_bad = verify_chain(chain, deck)
        problems += chain_problems
        bad_seats += chain_bad

    if n != len(CARDS):
        return AuditReport(ok=False, cards=[], bad_seats=sorted(set(bad_seats)),
                           problems=problems + [
                               f"deck has {n} positions, expected {len(CARDS)}"])
    if len(shares_by_seat) != len(seat_pubkeys):
        return AuditReport(ok=False, cards=[], bad_seats=sorted(set(bad_seats)),
                           problems=problems + [
                               "one share list per seat pubkey required"])

    # structural checks + trivial-ciphertext scan
    structural: List[str] = []
    for i, ct in enumerate(deck):
        if bytes(ct.c0) == bytes(R.IDENTITY):
            structural.append(f"position {i}: trivial ciphertext (invalid post-shuffle)")

    for s, shares in enumerate(shares_by_seat):
        if len(shares) != n:
            structural.append(f"seat {s}: {len(shares)} shares, expected {n}")
            bad_seats.append(s)

    if structural:
        return AuditReport(ok=False, cards=[], bad_seats=sorted(set(bad_seats)),
                           problems=problems + structural)

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
           "first_corrupt_round", "ShuffleRound", "ShuffleChain",
           "verify_shuffle_round", "verify_chain"]
