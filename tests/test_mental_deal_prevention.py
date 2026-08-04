"""Pins the Bayer-Groth prevention path in holdem/p2p/mental_deal.py.

Two properties carry this file. First, detection-only behaviour is
untouched: no proof is emitted, and an unsolicited one from a hostile or
misconfigured peer is ignored rather than fatal (otherwise anyone could
kill a table by attaching data nobody asked for). Second, with prevention
enabled a deck round is accepted ONLY against a valid proof bound to this
exact session, hand, round, shuffler seat, and commitment key -- and every
way of failing that check lands on the same deterministic abort, with a
reason string specific enough to tell the failures apart.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import bg_shuffle, bg_wire, elgamal as eg
    from holdem.p2p import mental_deal as md
    from holdem.p2p import ristretto as R
    from holdem.p2p import shuffle_mp
    from holdem.p2p.mental_deal import MentalDeal, Phase
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


# ------------------------------------------------------------ harness

def _secrets(seats):
    return {s: f"master-secret-of-seat-{s}".encode() for s in seats}


def run(seats, prevention=True, tamper=None, session="s", hand=1, button=0):
    """Drive a full peer-symmetric exchange, optionally tampering en route.

    Mirrors test_mental_deal.run_broadcast, plus a ``tamper(msg, deals)``
    hook that runs once per broadcast BEFORE any seat sees it -- so a
    single mutation is delivered to every peer, exactly as a cheating
    shuffler's message would be. Returning None drops the message.

    Returns (deals, delivered) where ``delivered`` is every message as the
    peers actually received it.
    """
    ms = _secrets(seats)
    deals = {
        s: MentalDeal(session_id=session, hand_no=hand, seat=s,
                      seats_in=list(seats), button=button,
                      master_secret=ms[s], prevention=prevention)
        for s in seats
    }
    delivered = []
    queue = []
    for s in seats:
        queue.extend(deals[s].start())

    while queue:
        msg = queue.pop(0)
        if tamper is not None:
            msg = tamper(msg, deals)
            if msg is None:
                continue
        delivered.append(msg)
        for s in seats:
            queue.extend(deals[s].handle(dict(msg)))
    return deals, delivered


def on_first_round(mutate):
    """Wrap ``mutate(msg, deals)`` so it only touches shuffle round 1.

    Round 1 is the useful target: its input deck is the canonical trivial
    deck, which every peer already holds, so a test can rebuild an honest
    shuffle of the same input without access to the shuffler's witness.
    """
    def tamper(msg, deals):
        if msg.get("type") == "deck_round" and msg.get("round") == 1:
            return mutate(dict(msg), deals)
        return msg
    return tamper


def reprove_round_one(deals, seats, msg, ctx_round, ctx_seat, ref=None):
    """Replace round 1 with an honest shuffle proved under a chosen context.

    The shuffle itself is perfectly valid; only the context binding is
    wrong. That isolates the binding from every other reason a proof might
    fail. ``ref`` supplies the context (default: a real participant), so a
    caller can bind to a different session or hand by passing a foreign
    MentalDeal.
    """
    participant = deals[seats[0]]
    ref = ref if ref is not None else participant
    pk = participant.joint_pk
    in_deck = eg.make_trivial_deck()
    deck, wit = shuffle_mp.shuffle_deck(pk, in_deck)
    proof = bg_shuffle.prove(
        pk, md.bg_commitment_key(), in_deck, deck, wit.perm, wit.scalars,
        md.BG_M, md.BG_N, ref._bg_ctx(ctx_round, ctx_seat))
    msg["deck"] = [ct.to_hex() for ct in deck]
    msg["proof"] = bg_wire.encode(proof)
    return msg


def deck_rounds(delivered):
    return [m for m in delivered if m.get("type") == "deck_round"]


def assert_aborted(deals, seats, fragment, bad_seat):
    """Every seat must land on the same deterministic abort."""
    for s in seats:
        d = deals[s]
        assert d.phase == Phase.ABORTED, f"seat {s} did not abort"
        assert fragment in d.abort_reason, \
            f"seat {s}: {d.abort_reason!r} lacks {fragment!r}"
        assert d.bad_seat == bad_seat, \
            f"seat {s} blamed {d.bad_seat}, expected {bad_seat}"


# ------------------------------------------- detection-only stays unchanged

@pytest.mark.parametrize("n", [2, 3])
def test_detection_only_emits_no_proof(n):
    seats = list(range(n))
    deals, delivered = run(seats, prevention=False)
    rounds = deck_rounds(delivered)
    assert len(rounds) == n
    for msg in rounds:
        assert "proof" not in msg
    for s in seats:
        assert deals[s].phase == Phase.DEAL
        assert deals[s].abort_reason is None


def test_detection_only_ignores_unsolicited_proof():
    """A prevention-enabled or malicious peer must not be able to kill a
    detection-only table by attaching data nobody requested."""
    def inject(msg, _deals):
        msg["proof"] = {"a_commits": "not-even-close"}
        return msg

    seats = [0, 1]
    deals, _ = run(seats, prevention=False, tamper=on_first_round(inject))
    for s in seats:
        assert deals[s].phase == Phase.DEAL
        assert deals[s].abort_reason is None


def test_detection_only_ignores_a_structurally_valid_proof():
    """Not just garbage -- even a well-formed proof for the wrong statement
    must be ignored when prevention is off."""
    def inject(msg, deals):
        return reprove_round_one(deals, [0, 1], msg, ctx_round=9, ctx_seat=7)

    seats = [0, 1]
    deals, _ = run(seats, prevention=False, tamper=on_first_round(inject))
    for s in seats:
        assert deals[s].phase == Phase.DEAL
        assert deals[s].abort_reason is None


# ---------------------------------------------------------- happy path

@pytest.mark.parametrize("n", [2, 3])
def test_prevention_completes_and_every_peer_converges(n):
    seats = list(range(n))
    deals, delivered = run(seats, prevention=True)

    rounds = deck_rounds(delivered)
    assert len(rounds) == n
    for msg in rounds:
        assert "proof" in msg
        bg_wire.decode(msg["proof"], md.BG_M, md.BG_N)   # raises if malformed

    reference = [bytes(ct.c0) + bytes(ct.c1) for ct in deals[seats[0]].deck]
    for s in seats:
        d = deals[s]
        assert d.abort_reason is None
        assert d.phase == Phase.DEAL
        assert d.is_shuffle_complete()
        assert [bytes(ct.c0) + bytes(ct.c1) for ct in d.deck] == reference


def test_prevention_completes_at_nine_seats():
    """Full table: nine proofs generated, each verified by all nine peers."""
    seats = list(range(9))
    deals, delivered = run(seats, prevention=True)
    assert len(deck_rounds(delivered)) == 9
    for s in seats:
        assert deals[s].abort_reason is None
        assert deals[s].is_shuffle_complete()


def test_round_one_proves_over_the_trivial_deck():
    """Round 1's input is the all-identity trivial deck. Nothing in the
    argument objects to that, but it is the one input every hand uses and
    the only one where c0 is the identity for all 52 entries."""
    in_deck = eg.make_trivial_deck()
    assert all(bytes(ct.c0) == bytes(R.IDENTITY) for ct in in_deck)

    deals, delivered = run([0, 1], prevention=True)
    first = deck_rounds(delivered)[0]
    assert first["round"] == 1
    proof = bg_wire.decode(first["proof"], md.BG_M, md.BG_N)
    out_deck = [eg.Ciphertext.from_hex(pair) for pair in first["deck"]]
    assert bg_shuffle.verify(
        deals[0].joint_pk, md.bg_commitment_key(), in_deck, out_deck,
        md.BG_M, md.BG_N, deals[0]._bg_ctx(1, first["seat"]), proof)


# ------------------------------------------------------------ rejections

def test_tampered_output_deck_is_rejected():
    """Swapping two output ciphertexts keeps every structural check happy
    -- 52 entries, all parseable, none trivial -- so only the proof can
    catch it."""
    def swap(msg, _deals):
        deck = list(msg["deck"])
        deck[0], deck[1] = deck[1], deck[0]
        msg["deck"] = deck
        return msg

    seats = [0, 1]
    deals, _ = run(seats, prevention=True, tamper=on_first_round(swap))
    assert_aborted(deals, seats, "invalid shuffle proof", bad_seat=0)


def test_tampered_proof_is_rejected():
    """Still decodable -- a valid point in a valid slot -- so this exercises
    verification rather than the codec."""
    def corrupt(msg, _deals):
        proof = dict(msg["proof"])
        commits = list(proof["a_commits"])
        commits[0] = bytes(R.G).hex()
        proof["a_commits"] = commits
        msg["proof"] = proof
        return msg

    seats = [0, 1]
    deals, _ = run(seats, prevention=True, tamper=on_first_round(corrupt))
    assert_aborted(deals, seats, "invalid shuffle proof", bad_seat=0)


def test_missing_proof_aborts_deterministically():
    def strip(msg, _deals):
        msg.pop("proof", None)
        return msg

    seats = [0, 1]
    deals, _ = run(seats, prevention=True, tamper=on_first_round(strip))
    assert_aborted(deals, seats, "omitted the required shuffle proof",
                   bad_seat=0)


def test_undecodable_proof_aborts_deterministically():
    def mangle(msg, _deals):
        msg["proof"] = {"a_commits": ["zz" * 32], "b_commits": [],
                        "product": {}, "multi": {}}
        return msg

    seats = [0, 1]
    deals, _ = run(seats, prevention=True, tamper=on_first_round(mangle))
    assert_aborted(deals, seats, "undecodable shuffle proof", bad_seat=0)


def test_proof_of_wrong_type_aborts_deterministically():
    def mangle(msg, _deals):
        msg["proof"] = "a string is not a proof"
        return msg

    seats = [0, 1]
    deals, _ = run(seats, prevention=True, tamper=on_first_round(mangle))
    assert_aborted(deals, seats, "undecodable shuffle proof", bad_seat=0)


# -------------------------------------------------- statement binding

def test_wrong_round_context_is_rejected():
    def wrong_round(msg, deals):
        return reprove_round_one(deals, [0, 1], msg, ctx_round=2, ctx_seat=0)

    seats = [0, 1]
    deals, _ = run(seats, prevention=True,
                   tamper=on_first_round(wrong_round))
    assert_aborted(deals, seats, "invalid shuffle proof", bad_seat=0)


def test_wrong_seat_context_is_rejected():
    def wrong_seat(msg, deals):
        return reprove_round_one(deals, [0, 1], msg, ctx_round=1, ctx_seat=1)

    seats = [0, 1]
    deals, _ = run(seats, prevention=True, tamper=on_first_round(wrong_seat))
    assert_aborted(deals, seats, "invalid shuffle proof", bad_seat=0)


@pytest.mark.parametrize("session,hand", [("other", 1), ("s", 2)])
def test_wrong_session_or_hand_context_is_rejected(session, hand):
    """A proof lifted from another session or another hand must not verify
    here, even though the shuffle it attests to is honest."""
    foreign = MentalDeal(session_id=session, hand_no=hand, seat=0,
                         seats_in=[0, 1], button=0, master_secret=b"x",
                         prevention=True)

    def wrong_binding(msg, deals):
        return reprove_round_one(deals, [0, 1], msg, ctx_round=1, ctx_seat=0,
                                 ref=foreign)

    seats = [0, 1]
    deals, _ = run(seats, prevention=True,
                   tamper=on_first_round(wrong_binding))
    assert_aborted(deals, seats, "invalid shuffle proof", bad_seat=0)


def test_bg_ctx_varies_with_every_bound_field():
    def deal(session="s", hand=1):
        return MentalDeal(session_id=session, hand_no=hand, seat=0,
                          seats_in=[0, 1], button=0, master_secret=b"m")

    base = deal()._bg_ctx(1, 0)
    assert base != deal()._bg_ctx(2, 0)             # round
    assert base != deal()._bg_ctx(1, 1)             # seat
    assert base != deal(hand=2)._bg_ctx(1, 0)       # hand
    assert base != deal(session="t")._bg_ctx(1, 0)  # session
    assert deal()._bg_ctx(1, 0) == base             # deterministic


def test_bg_ctx_binds_the_commitment_key_seed():
    ctx = MentalDeal(session_id="s", hand_no=1, seat=0, seats_in=[0, 1],
                     button=0, master_secret=b"m")._bg_ctx(1, 0)
    assert md.BG_CK_SEED in ctx


def test_commitment_key_is_nums_and_shared():
    """Every peer derives the same key from a public seed, so nothing is
    transmitted and nobody can hold a commitment trapdoor."""
    ck = md.bg_commitment_key()
    assert ck.verify_nums()
    assert ck is md.bg_commitment_key()             # cached
    assert ck.n == md.BG_N
    assert md.BG_M * md.BG_N == 52
