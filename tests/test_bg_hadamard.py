"""Pins holdem/p2p/bg_hadamard.py -- BG Hadamard product argument (5.1).

Two properties here are invisible to end-to-end verification and get
direct assertions instead:

* ``test_transcript_binds_c_B`` -- Theorem 9's extraction requires the
  partial-product commitments to be fixed before the challenges exist.
  Deriving y from a transcript that omits c_B breaks the argument while
  leaving every completeness test green.
* ``test_zero_argument_sequence_length_is_m`` -- the reduction hands the
  zero argument M = m, not m-1, because the -1 term occupies the last A
  slot. Getting this wrong proves a different, weaker statement.
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import ristretto as R
    from holdem.p2p import pedersen as P
    from holdem.p2p import bg_zero as Z
    from holdem.p2p import bg_hadamard as H
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


ZERO = R.Scalar(b"\x00" * 32)
CTX = b"session=1|hand=7|shuffle=0|round=2"


def _s(i: int) -> R.Scalar:
    return R.scalar_reduce(hashlib.sha512(f"had:{i}".encode()).digest())


def _bump(p: R.Point) -> R.Point:
    return R.add(p, R.mul_base(_s(2)))


def _tweak(sc: R.Scalar) -> R.Scalar:
    return R.scalar_add(sc, _s(1))


def _setup(m, n, seed=b"had-test", ctx=CTX):
    """A witness where b really is the entry-wise product of the a_i."""
    ck = P.CommitmentKey.generate(max(n, 2), seed=seed)
    a = [[_s(100 * (i + 1) + j) for j in range(n)] for i in range(m)]
    r = [_s(9000 + i) for i in range(m)]
    c_A = [P.commit(ck, a[i], r[i]) for i in range(m)]
    b = list(a[0])
    for i in range(1, m):
        b = [R.scalar_mul(b[j], a[i][j]) for j in range(n)]
    s = _s(9500)
    c_b = P.commit(ck, b, s)
    return ck, a, r, c_A, b, s, c_b, ctx


def _prove(setup):
    ck, a, r, c_A, b, s, c_b, ctx = setup
    return H.prove(ck, c_A, a, r, c_b, b, s, ctx)


def _verify(setup, proof, n):
    ck, _a, _r, c_A, _b, _sv, c_b, ctx = setup
    return H.verify(ck, c_A, c_b, n, ctx, proof)


# --------------------------------------------------------------- happy path

@pytest.mark.parametrize("m,n", [(2, 1), (2, 3), (3, 1), (3, 3), (4, 2),
                                 (5, 4), (6, 3)])
def test_valid_proof_verifies(m, n):
    setup = _setup(m, n)
    assert _verify(setup, _prove(setup), n)


def test_m_equals_two_has_no_interior_commitments():
    """c_B1 = c_A1 and c_Bm = c_b, so at m = 2 the whole chain is pinned
    by the statement and nothing is transmitted."""
    setup = _setup(2, 3)
    proof = _prove(setup)
    assert proof.c_B_interior == []
    assert _verify(setup, proof, 3)


@pytest.mark.parametrize("m", [2, 3, 5])
def test_interior_commitment_count(m):
    setup = _setup(m, 2)
    assert len(_prove(setup).c_B_interior) == m - 2


def test_proofs_are_randomised():
    setup = _setup(4, 3)
    p1, p2 = _prove(setup), _prove(setup)
    assert bytes(p1.c_B_interior[0]) != bytes(p2.c_B_interior[0])
    assert _verify(setup, p1, 3) and _verify(setup, p2, 3)


def test_zero_argument_sequence_length_is_m():
    """M = m, not m-1: the -1 term takes the last A slot.

    The zero proof carries 2M+1 diagonal commitments, so their count
    pins M directly. An implementation that passed m-1 would produce a
    self-consistent proof of a weaker statement.
    """
    for m in (2, 3, 5):
        proof = _prove(_setup(m, 2))
        assert len(proof.zero.c_D) == 2 * m + 1


def test_identity_slot_sits_at_m_plus_one():
    for m in (2, 4):
        proof = _prove(_setup(m, 2))
        assert bytes(proof.zero.c_D[m + 1]) == bytes(R.IDENTITY)


# ------------------------------------------------------- prover-side guards

def test_prove_rejects_wrong_product():
    ck, a, r, c_A, b, s, c_b, ctx = _setup(3, 3)
    b2 = list(b)
    b2[1] = _tweak(b2[1])
    c_b2 = P.commit(ck, b2, s)
    with pytest.raises(ValueError, match="does not satisfy"):
        H.prove(ck, c_A, a, r, c_b2, b2, s, ctx)


def test_prove_rejects_m_below_two():
    """Unlike bg_zero, which is fine at m = 1, the Hadamard argument
    degenerates: no partial products exist and the reduction is empty."""
    ck, a, r, c_A, b, s, c_b, ctx = _setup(2, 3)
    with pytest.raises(ValueError, match="m >= 2"):
        H.prove(ck, c_A[:1], a[:1], r[:1], c_b, b, s, ctx)


def test_prove_rejects_commitments_that_do_not_open():
    ck, a, r, c_A, b, s, c_b, ctx = _setup(3, 2)
    c_A[1] = _bump(c_A[1])
    with pytest.raises(ValueError, match="does not open"):
        H.prove(ck, c_A, a, r, c_b, b, s, ctx)


def test_prove_rejects_c_b_that_does_not_open():
    ck, a, r, c_A, b, s, c_b, ctx = _setup(3, 2)
    with pytest.raises(ValueError, match="c_b does not open"):
        H.prove(ck, c_A, a, r, _bump(c_b), b, s, ctx)


def test_prove_rejects_ragged_witness():
    ck, a, r, c_A, b, s, c_b, ctx = _setup(3, 3)
    a[1] = a[1][:-1]
    with pytest.raises(ValueError, match="length n"):
        H.prove(ck, c_A, a, r, c_b, b, s, ctx)


def test_prove_rejects_vectors_wider_than_the_key():
    ck = P.CommitmentKey.generate(2, seed=b"small")
    a = [[_s(i) for i in range(5)] for _ in range(2)]
    with pytest.raises(ValueError, match="wider than commitment key"):
        H.prove(ck, [R.IDENTITY] * 2, a, [ZERO] * 2, R.IDENTITY,
                a[0], ZERO, CTX)


# -------------------------------------------------------------- rejections

def test_tampered_interior_commitment_rejected():
    setup = _setup(4, 3)
    p = _prove(setup)
    interior = list(p.c_B_interior)
    interior[0] = _bump(interior[0])
    assert not _verify(setup, H.HadamardProof(interior, p.zero), 3)


def test_tampered_zero_proof_rejected():
    setup = _setup(3, 3)
    p = _prove(setup)
    z = p.zero
    broken = Z.ZeroProof(_bump(z.c_A0), z.c_Bm, z.c_D, z.a_tilde,
                         z.b_tilde, z.r_tilde, z.s_tilde, z.t_tilde)
    assert not _verify(setup, H.HadamardProof(p.c_B_interior, broken), 3)


def test_changed_statement_commitment_rejected():
    setup = _setup(3, 3)
    ck, _a, _r, c_A, _b, _sv, c_b, ctx = setup
    p = _prove(setup)
    tampered = list(c_A)
    tampered[1] = _bump(tampered[1])
    assert not H.verify(ck, tampered, c_b, 3, ctx, p)


def test_changed_product_commitment_rejected():
    """c_Bm IS c_b, so a different claimed product is a different chain
    endpoint, not merely a different statement label."""
    setup = _setup(3, 3)
    ck, _a, _r, c_A, _b, _sv, c_b, ctx = setup
    p = _prove(setup)
    assert not H.verify(ck, c_A, _bump(c_b), 3, ctx, p)


def test_reordered_a_commitments_rejected():
    """The chain is ordered: b_i depends on a_1..a_i. Swapping two
    statement commitments must not verify even though the Hadamard
    product itself is commutative."""
    setup = _setup(4, 3)
    ck, _a, _r, c_A, _b, _sv, c_b, ctx = setup
    p = _prove(setup)
    swapped = [c_A[1], c_A[0], *c_A[2:]]
    assert not H.verify(ck, swapped, c_b, 3, ctx, p)


def test_different_context_rejected():
    setup = _setup(3, 3)
    ck, _a, _r, c_A, _b, _sv, c_b, _ctx = setup
    p = _prove(setup)
    assert not H.verify(ck, c_A, c_b, 3, b"session=1|hand=8", p)


def test_different_commitment_key_rejected():
    setup = _setup(3, 3)
    _ck, _a, _r, c_A, _b, _sv, c_b, ctx = setup
    p = _prove(setup)
    other = P.CommitmentKey.generate(3, seed=b"other-seed")
    assert not H.verify(other, c_A, c_b, 3, ctx, p)


def test_declared_width_mismatch_rejected():
    setup = _setup(3, 3)
    ck, _a, _r, c_A, _b, _sv, c_b, ctx = setup
    p = _prove(setup)
    assert not H.verify(ck, c_A, c_b, 2, ctx, p)


def test_wrong_interior_length_rejected():
    setup = _setup(4, 3)
    p = _prove(setup)
    assert not _verify(setup, H.HadamardProof(p.c_B_interior[:-1], p.zero), 3)
    assert not _verify(
        setup, H.HadamardProof([*p.c_B_interior, R.IDENTITY], p.zero), 3)


def test_m_below_two_rejected_by_verifier():
    setup = _setup(3, 3)
    ck, _a, _r, c_A, _b, _sv, c_b, ctx = setup
    p = _prove(setup)
    assert not H.verify(ck, c_A[:1], c_b, 3, ctx, p)


def test_proof_does_not_transfer_between_statements():
    a = _setup(3, 3, seed=b"had-test", ctx=CTX)
    b = _setup(3, 3, seed=b"had-test", ctx=b"other-context")
    p = _prove(a)
    assert not _verify(b, p, 3)


# ------------------------------------------------------- direct transcript

def _t(setup, **kw):
    ck, _a, _r, c_A, _b, _sv, c_b, ctx = setup
    proof = kw.pop("proof")
    c_B = [c_A[0], *proof.c_B_interior, c_b]
    args = dict(ck=ck, n=3, m=len(c_A), context=ctx, c_A=c_A, c_b=c_b,
                c_B=c_B)
    args.update(kw)
    # _transcript returns the unfinalized hash object so both challenges
    # can draw from it. Finalize here: comparing hash objects with != would
    # compare identity and every binding assertion below would pass
    # vacuously.
    return H._transcript(args["ck"], args["n"], args["m"], args["context"],
                         args["c_A"], args["c_b"], args["c_B"]).digest()


def test_transcript_binds_c_B():
    """THE soundness-critical ordering property.

    Theorem 9's extraction needs d_1..d_{m-1}, d determined before the
    prover sees the y that defines the bilinear map. That holds only if
    c_B is inside the transcript both challenges are drawn from. Omitting
    it leaves every completeness and rejection test in this file passing.
    """
    setup = _setup(4, 3)
    p = _prove(setup)
    base = _t(setup, proof=p)
    c_A = setup[3]
    altered = [c_A[0], _bump(p.c_B_interior[0]), *p.c_B_interior[1:],
               setup[6]]
    assert _t(setup, proof=p, c_B=altered) != base


def test_transcript_binds_every_statement_field():
    setup = _setup(4, 3)
    ck, _a, _r, c_A, _b, _sv, c_b, ctx = setup
    p = _prove(setup)
    base = _t(setup, proof=p)
    variants = [
        _t(setup, proof=p, context=b"other"),
        _t(setup, proof=p, c_A=[_bump(c_A[0]), *c_A[1:]]),
        _t(setup, proof=p, c_b=_bump(c_b)),
        _t(setup, proof=p, n=2),
        _t(setup, proof=p, m=5),
        _t(setup, proof=p,
           ck=P.CommitmentKey.generate(ck.n, seed=b"other-seed")),
        # A substituted generator with H and the seed untouched: the key
        # must be hashed generator by generator, not as a seed reference.
        _t(setup, proof=p,
           ck=P.CommitmentKey(H=ck.H, Gs=[_bump(ck.Gs[0]), *ck.Gs[1:]],
                              seed=ck.seed)),
    ]
    for v in variants:
        assert v != base


def _h(data: bytes):
    """A transcript hash object primed with ``data``.

    _challenges takes the unfinalized hash so x and y can be drawn from
    one preimage under different labels.
    """
    h = hashlib.sha512()
    h.update(data)
    return h


def test_x_and_y_are_distinct_and_nonzero():
    x, y = H._challenges(_h(b"some-transcript"))
    assert not R.is_zero_scalar(x)
    assert not R.is_zero_scalar(y)
    assert bytes(x) != bytes(y)


def test_challenges_move_together_with_the_transcript():
    """Both are drawn from one transcript hash, so a prover regrinding
    for a favourable y necessarily regrinds x as well."""
    x1, y1 = H._challenges(_h(b"transcript-a"))
    x2, y2 = H._challenges(_h(b"transcript-b"))
    assert bytes(x1) != bytes(x2)
    assert bytes(y1) != bytes(y2)


def test_zero_context_binds_the_invocation():
    """The inner zero proof must not be liftable out of this Hadamard
    invocation and presented against another that reduces to the same
    relation."""
    setup = _setup(3, 3)
    ck, _a, _r, c_A, _b, _sv, c_b, ctx = setup
    p = _prove(setup)
    c_B = [c_A[0], *p.c_B_interior, c_b]
    x, y = _s(11), _s(12)
    base = H._zero_context(ctx, c_A, c_b, c_B, x, y)
    variants = [
        H._zero_context(b"other", c_A, c_b, c_B, x, y),
        H._zero_context(ctx, [_bump(c_A[0]), *c_A[1:]], c_b, c_B, x, y),
        H._zero_context(ctx, c_A, _bump(c_b), c_B, x, y),
        H._zero_context(ctx, c_A, c_b, [_bump(c_B[0]), *c_B[1:]], x, y),
        H._zero_context(ctx, c_A, c_b, c_B, _tweak(x), y),
        H._zero_context(ctx, c_A, c_b, c_B, x, _tweak(y)),
    ]
    for v in variants:
        assert v != base


def test_production_map_comes_from_the_challenge():
    """5.1's map is y^1..y^n from the verifier challenge, built with the
    production constructor -- never free-form coefficients."""
    _x, y = H._challenges(_h(b"t"))
    bmap = Z.BilinearMap.from_challenge(y, 4)
    assert bytes(bmap.coefficients[0]) == bytes(y)
    assert bmap.n == 4


def test_hadamard_and_zero_domains_differ():
    assert H._DOMAIN != Z._DOMAIN
