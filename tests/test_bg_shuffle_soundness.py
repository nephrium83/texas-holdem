"""Soundness tests for the Bayer-Groth shuffle argument.

test_bg_shuffle.py covers completeness (honest proofs verify) and
tamper-resistance (mutating a finished proof breaks it). Neither catches
the failure mode that actually matters: a prover who runs the honest
algorithm over a deck that is not a shuffle of the input at all.

That gap was not hypothetical. The argument shipped with the verifier
reading the multi-exponentiation statement out of the proof
(``product=proof.multi.vector_e_k[m]``) instead of recomputing it from the
public input deck, which made the check ``x != x`` and left in_deck and
out_deck linked only by the Fiat-Shamir hash. Every test in the suite
passed while a shuffler could replace the whole deck with 52 copies of one
card. These tests are written from the attacker's side so that regression
cannot recur silently.

The attacker here is ``bg_shuffle._prove_unchecked`` -- the real prover
minus its witness self-check, which is what an attacker would delete
first. Soundness must never depend on the prover policing itself.
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import bg_shuffle as S
    from holdem.p2p import elgamal as E
    from holdem.p2p import pedersen as P
    from holdem.p2p import ristretto as R
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


CTX = b"session=1|hand=7|shuffle=1"
M, N = 4, 13


def _s(i: int) -> R.Scalar:
    return R.scalar_reduce(hashlib.sha512(f"soundness:{i}".encode()).digest())


@pytest.fixture(scope="module")
def table():
    """A real 52-card statement: key, commitment key, and input deck."""
    ck = P.CommitmentKey.generate(N, seed=b"soundness-test")
    pk = R.mul_base(_s(1))
    in_deck = E.make_trivial_deck()
    perm = list(range(52))
    perm[0], perm[7] = perm[7], perm[0]
    scalars = [_s(200 + i) for i in range(52)]
    return {"ck": ck, "pk": pk, "in_deck": in_deck, "perm": perm,
            "scalars": scalars}


def forge(table, out_deck):
    """Run the honest prover over a chosen output deck, then verify.

    Returns the verifier's verdict. Anything but False for a deck that is
    not a re-encrypted permutation of the input is a soundness break.
    """
    proof = S._prove_unchecked(
        table["pk"], table["ck"], table["in_deck"], out_deck,
        table["perm"], table["scalars"], M, N, CTX)
    return S.verify(table["pk"], table["ck"], table["in_deck"], out_deck,
                    M, N, CTX, proof)


def honest_output(table):
    return [E.reencrypt(table["pk"], table["in_deck"][src],
                        table["scalars"][i])
            for i, src in enumerate(table["perm"])]


# ------------------------------------------------------------- control

def test_honest_shuffle_still_verifies(table):
    """Control. Without this, every rejection below could be vacuous."""
    assert forge(table, honest_output(table)) is True


# ------------------------------------------------- forged output decks

def test_deck_of_identical_cards_is_rejected(table):
    """The catastrophic cheat: 51 cards destroyed, one dealt 52 times."""
    ace = E._CARD_POINTS[0]
    forged = [E.encrypt(table["pk"], ace, table["scalars"][i])
              for i in range(52)]
    assert forge(table, forged) is False


def test_shuffle_of_a_foreign_deck_is_rejected(table):
    """An honest shuffle -- of the wrong input. The output is internally
    consistent, so only the in_deck binding can catch it."""
    other = [E.encrypt(table["pk"], point, _s(400 + i))
             for i, point in enumerate(E._CARD_POINTS)]
    forged = [E.reencrypt(table["pk"], other[src], table["scalars"][i])
              for i, src in enumerate(table["perm"])]
    assert forge(table, forged) is False


def test_single_substituted_card_is_rejected(table):
    """The realistic cheat: keep 51 honest cards, swap one for a better
    one. Detection must not require the deck to be wholesale wrong."""
    forged = honest_output(table)
    forged[3] = E.encrypt(table["pk"], E._CARD_POINTS[0], _s(501))
    assert forge(table, forged) is False


def test_duplicated_card_is_rejected(table):
    """Two positions decrypt to the same card; one card vanishes."""
    forged = honest_output(table)
    forged[10] = E.reencrypt(table["pk"], forged[11], _s(502))
    assert forge(table, forged) is False


def test_reordered_output_is_rejected(table):
    """Same multiset of cards, wrong positions for the committed
    permutation. The deck is a legal deck; the proof is still a lie."""
    forged = honest_output(table)
    forged[0], forged[1] = forged[1], forged[0]
    assert forge(table, forged) is False


def test_unencrypted_deck_is_rejected(table):
    """A shuffler that skips re-encryption entirely."""
    assert forge(table, [table["in_deck"][src] for src in table["perm"]]) \
        is False


# -------------------------------------------- the statement binding itself

def test_verifier_recomputes_the_product_from_the_input_deck(table):
    """Pins the specific regression.

    The multi-exponentiation statement must be sum_j x^{j+1} * in_deck[j],
    recomputed by the verifier. If it is ever read back out of the proof,
    this equality becomes a tautology and every forgery above starts
    passing.
    """
    out_deck = honest_output(table)
    proof = S._prove_unchecked(
        table["pk"], table["ck"], table["in_deck"], out_deck,
        table["perm"], table["scalars"], M, N, CTX)
    statement = S._statement_context(
        CTX, table["ck"], table["pk"], table["in_deck"], out_deck, M, N)
    x = S._challenge_x(statement, proof.a_commits)
    expected = S._dot_cipher(S._powers(x, M * N), table["in_deck"])

    # The honest proof's E_m must equal the independently derived value.
    assert proof.multi.vector_e_k[M] == expected

    # And _multi_verify must reject any other claimed product.
    out_chunks = S._chunks(out_deck, N)
    assert S._multi_verify(
        table["pk"], table["ck"], statement, proof.a_commits,
        proof.b_commits, out_chunks, expected, proof.multi, M, N) is True
    wrong = S._cipher_add(expected, E.encrypt(table["pk"], R.G, _s(603)))
    assert S._multi_verify(
        table["pk"], table["ck"], statement, proof.a_commits,
        proof.b_commits, out_chunks, wrong, proof.multi, M, N) is False


def test_multi_exponentiation_uses_the_x_power_commitments(table):
    """The sub-argument's exponents are the b matrix (x^perm), committed in
    b_commits. Verifying the opening against a_commits instead would prove
    a true statement about the wrong vector and leave the decks unlinked,
    so a_commits must NOT satisfy the opening check."""
    out_deck = honest_output(table)
    proof = S._prove_unchecked(
        table["pk"], table["ck"], table["in_deck"], out_deck,
        table["perm"], table["scalars"], M, N, CTX)
    statement = S._statement_context(
        CTX, table["ck"], table["pk"], table["in_deck"], out_deck, M, N)
    x = S._challenge_x(statement, proof.a_commits)
    expected = S._dot_cipher(S._powers(x, M * N), table["in_deck"])
    out_chunks = S._chunks(out_deck, N)

    # Swapping the two commitment vectors must break verification.
    assert S._multi_verify(
        table["pk"], table["ck"], statement, proof.b_commits,
        proof.a_commits, out_chunks, expected, proof.multi, M, N) is False


def test_verify_recomputes_target_from_statement_not_proof(table):
    """Pins defect 1 at the PUBLIC boundary, where it actually lived.

    test_verifier_recomputes_the_product_from_the_input_deck above calls
    _multi_verify directly and hands it a correctly derived ``expected``.
    That pins the sub-function, but the defect was in verify(), which
    sourced that argument from the proof. Reintroducing it changes
    nothing the sub-function test can see.

    So this one goes through verify(). The proof below is internally
    self-consistent -- the honest algorithm run over a deck that is not a
    shuffle of the input -- and its E_m is therefore the multi-
    exponentiation of the FORGED deck, not of in_deck. Every batched
    equation balances. The single thing that separates it from a valid
    proof is the equality E_m == sum_j x^{j+1} in_deck[j], and only a
    verifier that derives the right-hand side from the statement can
    check it.

    The break this is written against sources the target from the proof
    on BOTH sides. A one-sided break is not defect 1: ``product`` also
    feeds _multi_challenge, so changing only the verifier desynchronizes
    the challenge and the proof is rejected for an unrelated reason --
    a rejection that looks like detection and is not.
    """
    foreign = [E.encrypt(table["pk"], point, _s(700 + i))
               for i, point in enumerate(E._CARD_POINTS)]
    forged = [E.reencrypt(table["pk"], foreign[src], table["scalars"][i])
              for i, src in enumerate(table["perm"])]

    proof = S._prove_unchecked(
        table["pk"], table["ck"], table["in_deck"], forged,
        table["perm"], table["scalars"], M, N, CTX)

    statement = S._statement_context(
        CTX, table["ck"], table["pk"], table["in_deck"], forged, M, N)
    x = S._challenge_x(statement, proof.a_commits)
    target = S._dot_cipher(S._powers(x, M * N), table["in_deck"])

    # The premise: self-consistent proof, wrong target. If these ever
    # coincide the test proves nothing, so assert the gap explicitly.
    assert proof.multi.vector_e_k[M] != target, \
        "forged proof happened to hit the real target; test is vacuous"

    assert S.verify(table["pk"], table["ck"], table["in_deck"], forged,
                    M, N, CTX, proof) is False


def test_proof_is_bound_to_the_joint_public_key(table):
    """A proof is about a shuffle under one key. Verified under a different
    public key it must fail, or a proof could be lifted from another table's
    key ceremony."""
    out_deck = honest_output(table)
    proof = S._prove_unchecked(
        table["pk"], table["ck"], table["in_deck"], out_deck,
        table["perm"], table["scalars"], M, N, CTX)
    assert S.verify(table["pk"], table["ck"], table["in_deck"], out_deck,
                    M, N, CTX, proof) is True
    other_pk = R.mul_base(_s(4242))
    assert bytes(other_pk) != bytes(table["pk"])
    assert S.verify(other_pk, table["ck"], table["in_deck"], out_deck,
                    M, N, CTX, proof) is False


def test_proof_is_bound_to_the_commitment_key(table):
    """The statement hashes every generator, so a proof does not carry over
    to a different commitment key. Without this, a key whose trapdoor
    someone knows could be substituted at verification time."""
    out_deck = honest_output(table)
    proof = S._prove_unchecked(
        table["pk"], table["ck"], table["in_deck"], out_deck,
        table["perm"], table["scalars"], M, N, CTX)
    other_ck = P.CommitmentKey.generate(N, seed=b"a-different-seed")
    assert bytes(other_ck.H) != bytes(table["ck"].H)
    assert S.verify(table["pk"], other_ck, table["in_deck"], out_deck,
                    M, N, CTX, proof) is False


def test_commitment_key_is_nothing_up_my_sleeve(table):
    """Soundness of every argument rests on nobody knowing the discrete-log
    relations among the generators. verify_nums recomputes them from the
    public seed, which is what makes that checkable rather than trusted."""
    assert table["ck"].verify_nums() is True


def test_forged_proof_is_rejected_at_every_supported_layout(table):
    """The break was layout-independent; so is its regression test."""
    for m, n in ((2, 26), (4, 13), (13, 4)):
        ck = P.CommitmentKey.generate(n, seed=b"soundness-test-layout")
        forged = [E.encrypt(table["pk"], E._CARD_POINTS[0], _s(700 + i))
                  for i in range(52)]
        proof = S._prove_unchecked(
            table["pk"], ck, table["in_deck"], forged, table["perm"],
            table["scalars"], m, n, CTX)
        assert S.verify(table["pk"], ck, table["in_deck"], forged, m, n,
                        CTX, proof) is False, f"layout {m}x{n} accepted"
        honest = honest_output(table)
        good = S._prove_unchecked(
            table["pk"], ck, table["in_deck"], honest, table["perm"],
            table["scalars"], m, n, CTX)
        assert S.verify(table["pk"], ck, table["in_deck"], honest, m, n,
                        CTX, good) is True, f"layout {m}x{n} rejected honest"
