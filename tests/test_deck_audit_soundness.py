"""Adversarial tests for the DEFAULT (detection-only) protection path.

Prevention is opt-in; detection-only is what a table runs unless the host
turns prevention on. Its entire guarantee rests on two things: a DLEQ proof
that a seat's decryption share really used that seat's key, and a multiset
check that the final deck is exactly the canonical 52 cards. If either can
be defeated, the default mode protects nothing.

The existing test_deck_audit.py and test_dleq.py cover completeness and
simple tampering. These are written from the cheater's side: a lying
decryptor, a corrupt shuffler, a replayed share, a forged proof.
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import deck_audit as da
    from holdem.p2p import dleq
    from holdem.p2p import elgamal as eg
    from holdem.p2p import ristretto as R
    from holdem.p2p.shuffle_mp import shuffle_deck
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


N_SEATS = 3


def _s(i: int) -> R.Scalar:
    return R.scalar_reduce(hashlib.sha512(f"audit:{i}".encode()).digest())


@pytest.fixture(scope="module")
def table():
    """A shuffled 52-card deck under a 3-seat joint key, plus honest shares."""
    xs = [_s(10 + i) for i in range(N_SEATS)]
    pubs = [R.mul_base(x) for x in xs]
    pk = eg.joint_public_key(pubs)
    deck, _ = shuffle_deck(pk, eg.make_trivial_deck())
    shares = [da.make_shares(deck, x) for x in xs]
    return {"xs": xs, "pubs": pubs, "pk": pk, "deck": deck, "shares": shares}


def audit(table, deck=None, shares=None):
    return da.audit_deck(deck if deck is not None else table["deck"],
                         table["pubs"],
                         shares if shares is not None else table["shares"])


# ----------------------------------------------------------- control

def test_honest_audit_passes(table):
    report = audit(table)
    assert report.ok is True
    assert report.problems == []
    assert sorted(report.cards) == sorted(eg.CARDS)


# ------------------------------------------------- lying decryptors

def test_bogus_share_is_caught_and_named(table):
    """A seat that submits a wrong share must be identified, not merely
    cause a failure."""
    tampered = [list(s) for s in table["shares"]]
    bad = tampered[1][7]
    tampered[1][7] = da.PositionShare(
        share=R.add(bad.share, R.G), proof=bad.proof)
    report = audit(table, shares=tampered)
    assert report.ok is False
    assert report.bad_seats == [1]


def test_share_replayed_from_another_position_is_caught(table):
    """The proof is bound to that position's C0, so a share lifted from a
    different position must not verify."""
    tampered = [list(s) for s in table["shares"]]
    tampered[0][3] = tampered[0][9]
    report = audit(table, shares=tampered)
    assert report.ok is False
    assert report.bad_seats == [0]


def test_share_stolen_from_another_seat_is_caught(table):
    """The proof is bound to the seat's own public key."""
    tampered = [list(s) for s in table["shares"]]
    tampered[2] = list(table["shares"][0])
    report = audit(table, shares=tampered)
    assert report.ok is False
    assert report.bad_seats == [2]


def test_identity_shares_are_caught(table):
    """The cheapest possible forgery: contribute nothing and hope the
    plaintext still lands on a card."""
    identity = da.PositionShare(share=R.IDENTITY, proof=b"\x00" * 64)
    report = audit(table, shares=[table["shares"][0], table["shares"][1],
                                  [identity] * 52])
    assert report.ok is False
    assert report.bad_seats == [2]


def test_every_lying_seat_is_named(table):
    tampered = [list(s) for s in table["shares"]]
    for seat in (0, 2):
        bad = tampered[seat][0]
        tampered[seat][0] = da.PositionShare(
            share=R.add(bad.share, R.G), proof=bad.proof)
    report = audit(table, shares=tampered)
    assert report.bad_seats == [0, 2]


def test_wrong_share_count_is_structural(table):
    report = audit(table, shares=[table["shares"][0][:51],
                                  table["shares"][1], table["shares"][2]])
    assert report.ok is False
    assert report.bad_seats == [0]


# ------------------------------------------------ corrupt shufflers

def _reshare(deck, xs):
    return [da.make_shares(deck, x) for x in xs]


def test_substituted_card_is_detected(table):
    """The shuffler swaps one card for a better one and honestly decrypts
    the result. Only the multiset check can catch this."""
    deck = list(table["deck"])
    deck[4] = eg.encrypt(table["pk"], eg._CARD_POINTS[0], R.random_scalar())
    report = da.audit_deck(deck, table["pubs"], _reshare(deck, table["xs"]))
    assert report.ok is False
    assert any("duplicated" in p or "missing" in p for p in report.problems)


def test_duplicated_card_is_detected(table):
    deck = list(table["deck"])
    deck[11] = eg.reencrypt(table["pk"], deck[12], R.random_scalar())
    report = da.audit_deck(deck, table["pubs"], _reshare(deck, table["xs"]))
    assert report.ok is False
    assert any("duplicated" in p for p in report.problems)


def test_non_card_plaintext_is_detected(table):
    """A deck position that decrypts to a group element outside the card
    encoding must be reported, not silently dropped from the multiset."""
    stranger = R.hash_to_group(hashlib.sha512(b"not-a-card").digest())
    deck = list(table["deck"])
    deck[20] = eg.encrypt(table["pk"], stranger, R.random_scalar())
    report = da.audit_deck(deck, table["pubs"], _reshare(deck, table["xs"]))
    assert report.ok is False
    assert any("non-card point" in p for p in report.problems)
    assert any("missing" in p for p in report.problems)


def test_trivial_ciphertext_in_a_shuffled_deck_is_detected(table):
    """C0 = identity means the position was never re-encrypted; DLEQ over an
    identity base is degenerate, so it must be refused up front."""
    deck = list(table["deck"])
    deck[0] = eg.Ciphertext(R.IDENTITY, deck[0].c1)
    report = da.audit_deck(deck, table["pubs"], table["shares"])
    assert report.ok is False
    assert any("trivial ciphertext" in p for p in report.problems)


def test_make_shares_refuses_a_trivial_ciphertext(table):
    deck = list(table["deck"])
    deck[0] = eg.Ciphertext(R.IDENTITY, deck[0].c1)
    with pytest.raises(ValueError, match="trivial ciphertext"):
        da.make_shares(deck, table["xs"][0])


def test_a_whole_foreign_deck_is_detected(table):
    """52 valid cards, but not the deck that was played. The multiset check
    passes -- and that is correct: detection-only proves the FINAL deck is
    a legal deck, not that it descends from the input. That is exactly the
    gap Bayer-Groth prevention closes."""
    other, _ = shuffle_deck(table["pk"], eg.make_trivial_deck())
    report = da.audit_deck(other, table["pubs"], _reshare(other, table["xs"]))
    assert report.ok is True, (
        "a legal-but-foreign deck passes the audit by design; prevention "
        "mode is what binds output to input")


# ------------------------------------------------------ dleq forgery

def test_dleq_rejects_a_random_proof():
    x = R.random_scalar()
    C0 = R.mul_base(R.random_scalar())
    assert dleq.verify(R.mul_base(x), R.mul(x, C0), C0, b"\x11" * 64) is False


def test_dleq_rejects_a_wrong_length_proof():
    x = R.random_scalar()
    C0 = R.mul_base(R.random_scalar())
    good = dleq.prove(x, C0)
    for bad in (good[:63], good + b"\x00", b""):
        assert dleq.verify(R.mul_base(x), R.mul(x, C0), C0, bad) is False


def test_dleq_rejects_a_proof_for_a_different_base():
    """A proof made against one ciphertext must not transfer to another."""
    x = R.random_scalar()
    C0 = R.mul_base(R.random_scalar())
    other = R.mul_base(R.random_scalar())
    proof = dleq.prove(x, C0)
    assert dleq.verify(R.mul_base(x), R.mul(x, other), other, proof) is False


def test_dleq_rejects_a_mismatched_discrete_log():
    """The whole point: D computed with a different secret than X."""
    x, y = R.random_scalar(), R.random_scalar()
    C0 = R.mul_base(R.random_scalar())
    proof = dleq.prove(x, C0)
    assert dleq.verify(R.mul_base(x), R.mul(y, C0), C0, proof) is False


def test_dleq_refuses_the_identity_statement():
    """The DKG fell to a forged proof for the identity (x = 0). The
    analogous move here is claiming X = D = identity, and by the algebra it
    is a TRUE statement -- log_G(identity) == log_C0(identity) == 0 -- so a
    naive verifier would accept a proof that demonstrates nothing.

    It is refused, but only because verify uses R.mul rather than
    R.mul_safe: libsodium's contributory-behaviour check rejects scalarmult
    on the identity, and the except-ValueError path turns that into False.
    That is incidental, load-bearing behaviour. Switching those calls to
    mul_safe -- which several Bayer-Groth modules legitimately need -- would
    silently make this verify. Hence this test.
    """
    C0 = R.mul_base(R.random_scalar())
    zero = R.Scalar(b"\x00" * 32)
    k = R.random_scalar()
    R1, R2 = R.mul_base(k), R.mul(k, C0)
    c = dleq._challenge(R.IDENTITY, R.IDENTITY, C0, R1, R2)
    forged = bytes(c) + bytes(R.scalar_sub(k, R.scalar_mul(zero, c)))
    assert dleq.verify(R.IDENTITY, R.IDENTITY, C0, forged) is False


def test_identity_key_share_never_reaches_the_audit():
    """Defence in depth for the case above: even if DLEQ did accept an
    identity statement, the key ceremony refuses to seat such a peer."""
    from holdem.p2p import keygen_pop
    k = R.random_scalar()
    forged = bytes(R.mul_base(k)) + bytes(k)
    assert keygen_pop.verify(R.IDENTITY, forged, b"ctx") is False


# ------------------------------------------------------- attribution

def _cheating_table(prevention):
    """Run a hand in which the round-1 shuffler substitutes a card.

    Returns the seats' MentalDeal instances after the exchange settles.
    """
    from holdem.p2p.mental_deal import MentalDeal, Phase

    seats = [0, 1]
    deals = {
        s: MentalDeal(session_id="cheat", hand_no=1, seat=s,
                      seats_in=list(seats), button=0,
                      master_secret=f"secret-{s}".encode(),
                      prevention=prevention)
        for s in seats
    }
    queue = []
    for s in seats:
        queue.extend(deals[s].start())
    while queue:
        msg = queue.pop(0)
        if msg.get("type") == "deck_round" and msg.get("round") == 1:
            msg = dict(msg)
            pk = deals[0].joint_pk
            deck = [eg.Ciphertext.from_hex(pair) for pair in msg["deck"]]
            # Duplicate position 5 onto position 4: still 52 parseable,
            # non-trivial ciphertexts, so only the multiset check can see it.
            deck[4] = eg.reencrypt(pk, deck[5], R.random_scalar())
            msg["deck"] = [ct.to_hex() for ct in deck]
        for s in seats:
            queue.extend(deals[s].handle(dict(msg)))

    if all(deals[s].phase == Phase.DEAL for s in seats):
        queue = []
        for s in seats:
            queue.extend(deals[s].open_audit())
        while queue:
            msg = queue.pop(0)
            for s in seats:
                queue.extend(deals[s].handle(dict(msg)))
    return deals, Phase


def test_prevention_names_the_cheating_shuffler():
    """Prevention rejects the corrupt round as it arrives, so the shuffler
    is named before the deck is ever accepted."""
    deals, Phase = _cheating_table(prevention=True)
    for deal in deals.values():
        assert deal.phase == Phase.ABORTED
        assert deal.bad_seat == 0
        assert "shuffle proof" in deal.abort_reason


def test_detection_only_voids_the_hand_but_names_nobody():
    """The documented gap. The cheat IS caught -- the hand voids fail-closed
    and no card or chip is at risk -- but the multiset check only sees the
    final deck, so no seat is named. Attributing it needs every seat to open
    every intermediate round, which is a wire change and a pending design
    decision (see mental_deal's module docstring). If that exchange is ever
    added, this test should flip to asserting bad_seat == 0."""
    deals, Phase = _cheating_table(prevention=False)
    for deal in deals.values():
        assert deal.phase == Phase.ABORTED, "the cheat must still be caught"
        assert deal.audit_report is not None
        assert deal.audit_report.ok is False
        assert any("duplicated" in p for p in deal.audit_report.problems)
        assert deal.bad_seat is None, "documented: no attribution here yet"


def test_first_corrupt_round_is_implemented_but_unwired():
    """The analysis exists and works; nothing feeds it. Pinned so the two
    facts stay together -- a future wiring change should delete this test,
    not quietly leave the helper stranded."""
    bad = da.AuditReport(ok=False, cards=[], problems=["corrupt"])
    good = da.AuditReport(ok=True, cards=[])
    assert da.first_corrupt_round([good, bad, bad]) == 1
    assert da.first_corrupt_round([good, good]) is None

    source = (Path(__file__).resolve().parents[1]
              / "holdem" / "p2p" / "mental_deal.py").read_text()
    assert "first_corrupt_round(" not in source, (
        "mental_deal now calls first_corrupt_round -- wire the chain audit "
        "and update test_detection_only_voids_the_hand_but_names_nobody")
