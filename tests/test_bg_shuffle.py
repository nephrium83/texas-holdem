"""Tests for the standalone Bayer-Groth encrypted shuffle argument."""
import hashlib
import sys
from dataclasses import replace
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


def _s(i: int) -> R.Scalar:
    return R.scalar_reduce(hashlib.sha512(f"bg-shuffle:{i}".encode()).digest())


def _bump(point: R.Point) -> R.Point:
    return R.add(point, R.mul_base(_s(909)))


def _setup(m=2, n=2, context=CTX):
    ck = P.CommitmentKey.generate(n, seed=b"shuffle-proof-test")
    secret = _s(1)
    pk = R.mul_base(secret)
    messages = [R.hash_to_group(hashlib.sha512(f"message:{i}".encode()).digest())
                for i in range(m * n)]
    in_deck = [E.encrypt(pk, msg, _s(100 + i))
               for i, msg in enumerate(messages)]
    perm = list(range(m * n))
    perm[0], perm[1] = perm[1], perm[0]
    scalars = [_s(200 + i) for i in range(m * n)]
    out_deck = [E.reencrypt(pk, in_deck[src], scalars[i])
                for i, src in enumerate(perm)]
    return pk, ck, in_deck, out_deck, perm, scalars, m, n, context


def _prove(setup):
    pk, ck, ins, outs, perm, scalars, m, n, ctx = setup
    return S.prove(pk, ck, ins, outs, perm, scalars, m, n, ctx)


def test_valid_shuffle_proof_verifies():
    setup = _setup()
    pk, ck, ins, outs, _perm, _scalars, m, n, ctx = setup
    assert S.verify(pk, ck, ins, outs, m, n, ctx, _prove(setup))


def test_identity_shuffle_verifies():
    setup = _setup()
    pk, ck, ins, _outs, _perm, _scalars, m, n, ctx = setup
    perm = list(range(m * n))
    scalars = [_s(300 + i) for i in range(m * n)]
    outs = [E.reencrypt(pk, ins[i], scalars[i]) for i in perm]
    proof = S.prove(pk, ck, ins, outs, perm, scalars, m, n, ctx)
    assert S.verify(pk, ck, ins, outs, m, n, ctx, proof)


def test_four_by_two_shuffle_verifies():
    setup = _setup(m=4, n=2)
    pk, ck, ins, outs, _perm, _scalars, m, n, ctx = setup
    assert S.verify(pk, ck, ins, outs, m, n, ctx, _prove(setup))


def test_changed_context_rejected():
    setup = _setup()
    pk, ck, ins, outs, _perm, _scalars, m, n, _ctx = setup
    proof = _prove(setup)
    assert not S.verify(pk, ck, ins, outs, m, n, b"other", proof)


def test_changed_input_or_output_rejected():
    setup = _setup()
    pk, ck, ins, outs, _perm, _scalars, m, n, ctx = setup
    proof = _prove(setup)
    altered = list(ins)
    altered[0] = E.reencrypt(pk, altered[0], _s(999))
    assert not S.verify(pk, ck, altered, outs, m, n, ctx, proof)
    altered_out = list(outs)
    altered_out[0] = E.reencrypt(pk, altered_out[0], _s(998))
    assert not S.verify(pk, ck, ins, altered_out, m, n, ctx, proof)


def test_tampered_product_proof_rejected():
    setup = _setup()
    pk, ck, ins, outs, _perm, _scalars, m, n, ctx = setup
    proof = _prove(setup)
    tampered = replace(proof.product, c_b=R.add(proof.product.c_b, R.mul_base(_s(777))))
    proof = replace(proof, product=tampered)
    assert not S.verify(pk, ck, ins, outs, m, n, ctx, proof)


def test_tampered_multi_proof_rejected():
    setup = _setup()
    pk, ck, ins, outs, _perm, _scalars, m, n, ctx = setup
    proof = _prove(setup)
    tampered = replace(proof.multi, a_blinded=[_s(123), *proof.multi.a_blinded[1:]])
    proof = replace(proof, multi=tampered)
    assert not S.verify(pk, ck, ins, outs, m, n, ctx, proof)


def test_multi_transcript_binds_every_round_one_message():
    setup = _setup()
    pk, ck, ins, outs, _perm, _scalars, m, n, ctx = setup
    proof = _prove(setup)
    statement = S._statement_context(ctx, ck, pk, ins, outs, m, n)
    base = S._multi_challenge(
        statement, proof.a_commits, proof.b_commits,
        proof.multi.vector_e_k[m], proof.multi.a_0_commit,
        proof.multi.commit_b_k, proof.multi.vector_e_k)
    variants = [
        S._multi_challenge(
            statement, [_bump(proof.a_commits[0]), *proof.a_commits[1:]],
            proof.b_commits, proof.multi.vector_e_k[m],
            proof.multi.a_0_commit, proof.multi.commit_b_k,
            proof.multi.vector_e_k),
        S._multi_challenge(
            statement, proof.a_commits,
            [_bump(proof.b_commits[0]), *proof.b_commits[1:]],
            proof.multi.vector_e_k[m], proof.multi.a_0_commit,
            proof.multi.commit_b_k, proof.multi.vector_e_k),
        S._multi_challenge(
            statement, proof.a_commits, proof.b_commits,
            proof.multi.vector_e_k[m], _bump(proof.multi.a_0_commit),
            proof.multi.commit_b_k, proof.multi.vector_e_k),
    ]
    assert all(bytes(value) != bytes(base) for value in variants)


def test_wrong_witness_rejected_by_prover():
    setup = _setup()
    pk, ck, ins, outs, perm, scalars, m, n, ctx = setup
    wrong = list(perm)
    wrong[0], wrong[1] = wrong[1], wrong[0]
    with pytest.raises(ValueError, match="output deck"):
        S.prove(pk, ck, ins, outs, wrong, scalars, m, n, ctx)


@pytest.mark.parametrize("m,n", [(1, 4), (2, 1)])
def test_invalid_dimensions_rejected(m, n):
    setup = _setup(2, 2)
    pk, ck, ins, outs, perm, scalars, _m, _n, ctx = setup
    with pytest.raises(ValueError):
        S.prove(pk, ck, ins[:m * n], outs[:m * n], perm[:m * n],
                scalars[:m * n], m, n, ctx)
