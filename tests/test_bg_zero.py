"""Pins holdem/p2p/bg_zero.py -- BG zero argument (5.2, Theorem 10).

Soundness focus: a handful of happy paths, then rejection of every forgery
avenue. Two tests exist for specific mistakes that are invisible to
completeness testing and would otherwise ship silently:

* ``test_transcript_binds_every_prover_message`` is the 2019 Swiss Post
  incomplete-transcript failure. It asserts directly on the challenge,
  because the end-to-end tests do NOT catch an omitted field: dropping the
  bilinear map from the hash still leaves the whole suite green, since a
  changed map also changes the arithmetic of verification equation (3).
  Verified by removing the map from _challenge and re-running.
* ``test_rotated_b_commitments_rejected`` pins the ORDER of the b
  commitments. It does not, on its own, catch a reversed folding
  direction -- flipping x^{m-j} to x^j in both prover and verifier breaks
  ten completeness tests, so the happy paths are what guard that.
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
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


ZERO = R.Scalar(b"\x00" * 32)
CTX = b"session=1|hand=7|shuffle=0"


def _s(i: int) -> R.Scalar:
    """Deterministic scalar, so failures reproduce."""
    return R.scalar_reduce(hashlib.sha512(f"zero:{i}".encode()).digest())


def _setup(m, n, seed=b"zero-test", ctx=CTX, y_index=99):
    """A witness satisfying sum_{i=1..m} a_i * b_{i-1} = 0.

    Built by CANCELLATION rather than by zeroing operands: everything is
    picked deterministically, then the final coordinate of the last b
    vector is solved for. A relation that holds only because its operands
    are zero would not exercise the argument.
    """
    ck = P.CommitmentKey.generate(max(n, 2), seed=seed)
    bmap = Z.BilinearMap.from_challenge(_s(y_index), n)

    a = [[_s(100 * (i + 1) + j) for j in range(n)] for i in range(m)]
    b = [[_s(500 * (i + 1) + j) for j in range(n)] for i in range(m)]

    # Solve b[m-1][n-1] so the weighted sum cancels to zero.
    partial = ZERO
    for i in range(m):
        for j in range(n):
            if i == m - 1 and j == n - 1:
                continue
            partial = R.scalar_add(
                partial,
                R.scalar_mul(bmap.coefficients[j],
                             R.scalar_mul(a[i][j], b[i][j])),
            )
    denom = R.scalar_mul(bmap.coefficients[n - 1], a[m - 1][n - 1])
    b[m - 1][n - 1] = R.scalar_mul(R.scalar_negate(partial),
                                   R.scalar_invert(denom))

    r = [_s(9000 + i) for i in range(m)]
    s = [_s(9500 + i) for i in range(m)]
    c_A = [P.commit(ck, a[i], r[i]) for i in range(m)]
    c_B = [P.commit(ck, b[i], s[i]) for i in range(m)]
    return ck, bmap, a, r, b, s, c_A, c_B, ctx


def _prove(setup):
    ck, bmap, a, r, b, s, c_A, c_B, ctx = setup
    return Z.prove(ck, c_A, a, r, c_B, b, s, bmap, ctx)


def _verify(setup, proof):
    ck, bmap, _av, _rv, _bv, _sv, c_A, c_B, ctx = setup
    return Z.verify(ck, c_A, c_B, bmap, ctx, proof)


def _tweak(sc: R.Scalar) -> R.Scalar:
    return R.scalar_add(sc, _s(1))


def _bump(p: R.Point) -> R.Point:
    return R.add(p, R.mul_base(_s(2)))


# --------------------------------------------------------------- happy path

@pytest.mark.parametrize("m,n", [(1, 1), (1, 2), (2, 1), (2, 3), (3, 4),
                                 (4, 2), (5, 3)])
def test_valid_proof_verifies(m, n):
    setup = _setup(m, n)
    assert _verify(setup, _prove(setup))


def test_m_equals_one_is_supported():
    """Unlike bg_svp's n >= 2, section 5.2 does not collapse at the minimum.

    Its only forced quantity is t_{m+1}, which never needs to be free
    because d_{m+1} is publicly zero; the blinders a_0 and b_m stay
    unconstrained. See the module docstring.
    """
    setup = _setup(1, 3)
    assert _verify(setup, _prove(setup))


def test_proofs_are_randomised():
    setup = _setup(3, 3)
    p1, p2 = _prove(setup), _prove(setup)
    assert bytes(p1.c_A0) != bytes(p2.c_A0)
    assert _verify(setup, p1) and _verify(setup, p2)


def test_zero_valued_vectors_supported():
    """All-zero witnesses satisfy the relation trivially and must still
    produce a verifying proof -- the commitment path has to tolerate zero
    scalars throughout."""
    ck = P.CommitmentKey.generate(4, seed=b"zero-test")
    n, m = 3, 2
    bmap = Z.BilinearMap.from_challenge(_s(99), n)
    a = [[ZERO] * n for _ in range(m)]
    b = [[ZERO] * n for _ in range(m)]
    r = [_s(1) for _ in range(m)]
    s = [_s(2) for _ in range(m)]
    c_A = [P.commit(ck, a[i], r[i]) for i in range(m)]
    c_B = [P.commit(ck, b[i], s[i]) for i in range(m)]
    proof = Z.prove(ck, c_A, a, r, c_B, b, s, bmap, CTX)
    assert Z.verify(ck, c_A, c_B, bmap, CTX, proof)


def test_zero_slot_is_the_identity():
    m = 3
    setup = _setup(m, 2)
    proof = _prove(setup)
    assert bytes(proof.c_D[m + 1]) == bytes(R.IDENTITY)
    assert len(proof.c_D) == 2 * m + 1


# ------------------------------------------------------- prover-side guards

def test_prove_rejects_a_nonzero_relation():
    ck, bmap, a, r, b, s, c_A, c_B, ctx = _setup(3, 3)
    b[0][0] = R.scalar_add(b[0][0], _s(7))          # breaks cancellation
    c_B[0] = P.commit(ck, b[0], s[0])
    with pytest.raises(ValueError, match="does not satisfy"):
        Z.prove(ck, c_A, a, r, c_B, b, s, bmap, ctx)


def test_prove_rejects_m_below_one():
    ck, bmap, *_ = _setup(1, 2)
    with pytest.raises(ValueError, match="m >= 1"):
        Z.prove(ck, [], [], [], [], [], [], bmap, CTX)


def test_prove_rejects_commitments_that_do_not_open():
    ck, bmap, a, r, b, s, c_A, c_B, ctx = _setup(2, 2)
    c_A[0] = _bump(c_A[0])
    with pytest.raises(ValueError, match="does not open"):
        Z.prove(ck, c_A, a, r, c_B, b, s, bmap, ctx)


def test_prove_rejects_wrong_width_witness():
    ck, bmap, a, r, b, s, c_A, c_B, ctx = _setup(2, 3)
    a[0] = a[0][:-1]
    with pytest.raises(ValueError, match="length n"):
        Z.prove(ck, c_A, a, r, c_B, b, s, bmap, ctx)


def test_prove_rejects_vectors_wider_than_the_key():
    ck = P.CommitmentKey.generate(2, seed=b"small")
    bmap = Z.BilinearMap.from_challenge(_s(99), 5)
    a = [[_s(i) for i in range(5)]]
    b = [[_s(50 + i) for i in range(5)]]
    with pytest.raises(ValueError, match="wider than commitment key"):
        Z.prove(ck, [R.IDENTITY], a, [ZERO], [R.IDENTITY], b, [ZERO],
                bmap, CTX)


# ---------------------------------------------------- the two headline tests

def test_rotated_b_commitments_rejected():
    """The proof is bound to the ORDER of the b commitments.

    The b side folds with x^{m-j}, not x^j, so position carries meaning.
    A consistent direction flip in both prover and verifier is caught by
    the completeness tests rather than here; this pins that a verifier
    cannot be handed the same commitments in a different order.
    """
    setup = _setup(4, 3)
    ck, bmap, _av, _rv, _bv, _sv, c_A, c_B, ctx = setup
    proof = _prove(setup)
    rotated = [c_B[-1], *c_B[:-1]]
    assert not Z.verify(ck, c_A, rotated, bmap, ctx, proof)


def test_proof_does_not_verify_under_a_different_map():
    """A proof must not travel between maps -- the map defines the relation.

    Note this passes even if the map is left out of the Fiat-Shamir
    transcript entirely, because a different map also changes the value of
    a~ * b~ in verification equation (3). The transcript binding itself is
    pinned by test_transcript_binds_every_prover_message; this is the
    end-to-end statement of the same property.
    """
    setup = _setup(3, 3)
    ck, bmap, _av, _rv, _bv, _sv, c_A, c_B, ctx = setup
    proof = _prove(setup)
    other = Z.BilinearMap.from_challenge(_s(1234), bmap.n)
    assert not Z.verify(ck, c_A, c_B, other, ctx, proof)


def test_single_changed_map_coefficient_rejected():
    setup = _setup(3, 3)
    ck, bmap, _av, _rv, _bv, _sv, c_A, c_B, ctx = setup
    proof = _prove(setup)
    coeffs = list(bmap.coefficients)
    coeffs[1] = _tweak(coeffs[1])
    assert not Z.verify(ck, c_A, c_B, Z.BilinearMap(tuple(coeffs)), ctx,
                        proof)


# ------------------------------------------------------------- forged zeros

def test_forged_zero_slot_rejected():
    """c_D[m+1] must BE the identity, not merely open to zero.

    Recomputing it from a prover-supplied randomness would let a nonzero
    d_{m+1} through under blinding, which is the entire relation.
    """
    m = 3
    setup = _setup(m, 2)
    ck = setup[0]
    proof = _prove(setup)
    forged = list(proof.c_D)
    forged[m + 1] = P.commit(ck, [_s(3)], _s(4))    # opens to nonzero
    assert not _verify(setup, _replace_c_d(proof, forged))


def test_zero_slot_with_nonzero_randomness_rejected():
    m = 2
    setup = _setup(m, 2)
    ck = setup[0]
    proof = _prove(setup)
    forged = list(proof.c_D)
    forged[m + 1] = P.commit(ck, [ZERO], _s(5))     # opens to zero, not identity
    assert not _verify(setup, _replace_c_d(proof, forged))


def _replace_c_d(proof, c_d):
    return Z.ZeroProof(c_A0=proof.c_A0, c_Bm=proof.c_Bm, c_D=c_d,
                       a_tilde=proof.a_tilde, b_tilde=proof.b_tilde,
                       r_tilde=proof.r_tilde, s_tilde=proof.s_tilde,
                       t_tilde=proof.t_tilde)


# ------------------------------------------------------ tampered proof parts

def test_tampered_c_A0_rejected():
    setup = _setup(3, 2)
    p = _prove(setup)
    assert not _verify(setup, Z.ZeroProof(
        _bump(p.c_A0), p.c_Bm, p.c_D, p.a_tilde, p.b_tilde,
        p.r_tilde, p.s_tilde, p.t_tilde))


def test_tampered_c_Bm_rejected():
    setup = _setup(3, 2)
    p = _prove(setup)
    assert not _verify(setup, Z.ZeroProof(
        p.c_A0, _bump(p.c_Bm), p.c_D, p.a_tilde, p.b_tilde,
        p.r_tilde, p.s_tilde, p.t_tilde))


@pytest.mark.parametrize("k", [0, 1, 2, 5, 6])
def test_tampered_c_D_entry_rejected(k):
    setup = _setup(3, 2)                             # 2m+1 == 7 entries
    p = _prove(setup)
    c_d = list(p.c_D)
    c_d[k] = _bump(c_d[k])
    assert not _verify(setup, _replace_c_d(p, c_d))


def test_tampered_a_tilde_rejected():
    setup = _setup(3, 3)
    p = _prove(setup)
    a_t = list(p.a_tilde)
    a_t[1] = _tweak(a_t[1])
    assert not _verify(setup, Z.ZeroProof(
        p.c_A0, p.c_Bm, p.c_D, a_t, p.b_tilde,
        p.r_tilde, p.s_tilde, p.t_tilde))


def test_tampered_b_tilde_rejected():
    setup = _setup(3, 3)
    p = _prove(setup)
    b_t = list(p.b_tilde)
    b_t[2] = _tweak(b_t[2])
    assert not _verify(setup, Z.ZeroProof(
        p.c_A0, p.c_Bm, p.c_D, p.a_tilde, b_t,
        p.r_tilde, p.s_tilde, p.t_tilde))


def test_tampered_r_tilde_rejected():
    setup = _setup(2, 2)
    p = _prove(setup)
    assert not _verify(setup, Z.ZeroProof(
        p.c_A0, p.c_Bm, p.c_D, p.a_tilde, p.b_tilde,
        _tweak(p.r_tilde), p.s_tilde, p.t_tilde))


def test_tampered_s_tilde_rejected():
    setup = _setup(2, 2)
    p = _prove(setup)
    assert not _verify(setup, Z.ZeroProof(
        p.c_A0, p.c_Bm, p.c_D, p.a_tilde, p.b_tilde,
        p.r_tilde, _tweak(p.s_tilde), p.t_tilde))


def test_tampered_t_tilde_rejected():
    setup = _setup(2, 2)
    p = _prove(setup)
    assert not _verify(setup, Z.ZeroProof(
        p.c_A0, p.c_Bm, p.c_D, p.a_tilde, p.b_tilde,
        p.r_tilde, p.s_tilde, _tweak(p.t_tilde)))


# ---------------------------------------------------------- wrong statement

def test_changed_public_a_commitment_rejected():
    setup = _setup(3, 2)
    ck, bmap, _av, _rv, _bv, _sv, c_A, c_B, ctx = setup
    p = _prove(setup)
    tampered = list(c_A)
    tampered[1] = _bump(tampered[1])
    assert not Z.verify(ck, tampered, c_B, bmap, ctx, p)


def test_changed_public_b_commitment_rejected():
    setup = _setup(3, 2)
    ck, bmap, _av, _rv, _bv, _sv, c_A, c_B, ctx = setup
    p = _prove(setup)
    tampered = list(c_B)
    tampered[0] = _bump(tampered[0])
    assert not Z.verify(ck, c_A, tampered, bmap, ctx, p)


def test_different_context_rejected():
    setup = _setup(3, 2)
    ck, bmap, _av, _rv, _bv, _sv, c_A, c_B, _ctx = setup
    p = _prove(setup)
    assert not Z.verify(ck, c_A, c_B, bmap, b"session=1|hand=8|shuffle=0", p)


def test_different_commitment_key_rejected():
    setup = _setup(3, 2)
    _ck, bmap, _av, _rv, _bv, _sv, c_A, c_B, ctx = setup
    p = _prove(setup)
    other = P.CommitmentKey.generate(3, seed=b"other-seed")
    assert not Z.verify(other, c_A, c_B, bmap, ctx, p)


def test_substituted_generator_fails_verify_nums():
    """The anti-trapdoor property the whole construction rests on: hashing
    a key into Fiat-Shamir binds the proof to it but does not make it
    honest, so the key itself must be checkable."""
    ck = P.CommitmentKey.generate(4, seed=b"zero-test")
    tampered = P.CommitmentKey(H=ck.H, Gs=[_bump(ck.Gs[0]), *ck.Gs[1:]],
                               seed=ck.seed)
    assert ck.verify_nums()
    assert not tampered.verify_nums()


# -------------------------------------------------------- malformed shapes

def test_truncated_c_D_rejected():
    setup = _setup(3, 2)
    p = _prove(setup)
    assert not _verify(setup, _replace_c_d(p, p.c_D[:-1]))


def test_extra_c_D_entry_rejected():
    setup = _setup(3, 2)
    p = _prove(setup)
    assert not _verify(setup, _replace_c_d(p, [*p.c_D, R.IDENTITY]))


def test_truncated_a_tilde_rejected():
    setup = _setup(3, 3)
    p = _prove(setup)
    assert not _verify(setup, Z.ZeroProof(
        p.c_A0, p.c_Bm, p.c_D, p.a_tilde[:-1], p.b_tilde,
        p.r_tilde, p.s_tilde, p.t_tilde))


def test_mismatched_commitment_counts_rejected():
    setup = _setup(3, 2)
    ck, bmap, _av, _rv, _bv, _sv, c_A, c_B, ctx = setup
    p = _prove(setup)
    assert not Z.verify(ck, c_A, c_B[:-1], bmap, ctx, p)


def test_declared_width_wider_than_key_rejected():
    setup = _setup(2, 2)
    ck, _bmap, _av, _rv, _bv, _sv, c_A, c_B, ctx = setup
    p = _prove(setup)
    wide = Z.BilinearMap.from_challenge(_s(99), ck.n + 1)
    assert not Z.verify(ck, c_A, c_B, wide, ctx, p)


# --------------------------------------------------------- bilinear map API

def test_from_challenge_rejects_zero_y():
    with pytest.raises(ValueError, match="nonzero"):
        Z.BilinearMap.from_challenge(ZERO, 3)


def test_map_rejects_zero_coefficients():
    """A zero coefficient blanks a coordinate out of the relation, making it
    satisfiable without the witness holding."""
    with pytest.raises(ValueError, match="nonzero"):
        Z.BilinearMap((_s(1), ZERO, _s(3)))


def test_map_rejects_empty_coefficients():
    with pytest.raises(ValueError, match="at least one"):
        Z.BilinearMap(())


def test_from_challenge_exponents_start_at_one():
    """Section 5.1's map is sum_j u_j v_j y^j with j from 1, so the first
    coordinate carries y, not y^0 = 1. Starting at y^0 would leave it
    unweighted."""
    y = _s(42)
    bmap = Z.BilinearMap.from_challenge(y, 3)
    assert bytes(bmap.coefficients[0]) == bytes(y)
    assert bytes(bmap.coefficients[1]) == bytes(R.scalar_mul(y, y))
    assert bytes(bmap.coefficients[2]) == bytes(
        R.scalar_mul(R.scalar_mul(y, y), y))


def test_map_is_bilinear():
    n = 4
    bmap = Z.BilinearMap.from_challenge(_s(7), n)
    u = [_s(10 + i) for i in range(n)]
    v = [_s(20 + i) for i in range(n)]
    w = [_s(30 + i) for i in range(n)]
    left = bmap.evaluate(u, [R.scalar_add(v[i], w[i]) for i in range(n)])
    right = R.scalar_add(bmap.evaluate(u, v), bmap.evaluate(u, w))
    assert bytes(left) == bytes(right)


def test_map_encoding_distinguishes_maps():
    a = Z.BilinearMap.from_challenge(_s(1), 3)
    b = Z.BilinearMap.from_challenge(_s(2), 3)
    assert a.to_bytes() != b.to_bytes()


# ------------------------------------------------------------- transcript

def test_transcript_binds_every_prover_message():
    """Every element the verifier checks must be inside the hash: changing
    any of them must move the challenge."""
    setup = _setup(3, 2)
    ck, bmap, _av, _rv, _bv, _sv, c_A, c_B, ctx = setup
    p = _prove(setup)
    base = Z._challenge(ck, bmap.n, len(c_A), bmap, ctx, c_A, c_B,
                        p.c_A0, p.c_Bm, p.c_D)
    variants = [
        Z._challenge(ck, bmap.n, len(c_A), bmap, b"other", c_A, c_B,
                     p.c_A0, p.c_Bm, p.c_D),
        Z._challenge(ck, bmap.n, len(c_A), bmap, ctx,
                     [_bump(c_A[0]), *c_A[1:]], c_B, p.c_A0, p.c_Bm, p.c_D),
        Z._challenge(ck, bmap.n, len(c_A), bmap, ctx, c_A,
                     [_bump(c_B[0]), *c_B[1:]], p.c_A0, p.c_Bm, p.c_D),
        Z._challenge(ck, bmap.n, len(c_A), bmap, ctx, c_A, c_B,
                     _bump(p.c_A0), p.c_Bm, p.c_D),
        Z._challenge(ck, bmap.n, len(c_A), bmap, ctx, c_A, c_B,
                     p.c_A0, _bump(p.c_Bm), p.c_D),
        Z._challenge(ck, bmap.n, len(c_A), bmap, ctx, c_A, c_B,
                     p.c_A0, p.c_Bm, [_bump(p.c_D[0]), *p.c_D[1:]]),
        Z._challenge(ck, bmap.n + 1, len(c_A), bmap, ctx, c_A, c_B,
                     p.c_A0, p.c_Bm, p.c_D),
        Z._challenge(ck, bmap.n, len(c_A) + 1, bmap, ctx, c_A, c_B,
                     p.c_A0, p.c_Bm, p.c_D),
        # The map itself. This is the field whose omission is invisible to
        # every end-to-end test in this file -- see the module docstring.
        Z._challenge(ck, bmap.n, len(c_A),
                     Z.BilinearMap.from_challenge(_s(4321), bmap.n),
                     ctx, c_A, c_B, p.c_A0, p.c_Bm, p.c_D),
        # A SUBSTITUTED GENERATOR, with H and the seed left alone. The key
        # must be hashed generator by generator, not as a seed reference:
        # hashing only H (or only the seed) leaves this variant colliding
        # with the base challenge, and the end-to-end tests do not notice
        # because swapping the whole key also moves the commitments.
        Z._challenge(P.CommitmentKey(H=ck.H,
                                     Gs=[_bump(ck.Gs[0]), *ck.Gs[1:]],
                                     seed=ck.seed),
                     bmap.n, len(c_A), bmap, ctx, c_A, c_B,
                     p.c_A0, p.c_Bm, p.c_D),
    ]
    for v in variants:
        assert bytes(v) != bytes(base)


def test_transcript_is_length_prefixed():
    """Without length prefixes, a shorter context followed by a longer
    commitment list could share a preimage with the reverse. Dimensions are
    hashed first so this is belt-and-braces, but the property should hold
    on its own."""
    setup = _setup(2, 2)
    ck, bmap, _av, _rv, _bv, _sv, c_A, c_B, _ctx = setup
    p = _prove(setup)
    one = Z._challenge(ck, bmap.n, len(c_A), bmap, b"ab", c_A, c_B,
                       p.c_A0, p.c_Bm, p.c_D)
    two = Z._challenge(ck, bmap.n, len(c_A), bmap, b"a", c_A, c_B,
                       p.c_A0, p.c_Bm, p.c_D)
    assert bytes(one) != bytes(two)


def test_zero_challenge_is_rejected(monkeypatch):
    """A challenge of zero would collapse the folding; bg_svp guards the
    same way. Forced here because it is otherwise a ~2^-252 event."""
    setup = _setup(2, 2)
    p = _prove(setup)
    monkeypatch.setattr(Z.R, "scalar_reduce", lambda _d: ZERO)
    assert not _verify(setup, p)


def test_svp_and_zero_domains_differ():
    """A proof for one relation must never be replayable as another merely
    because the group elements happen to fit."""
    from holdem.p2p import bg_svp
    assert Z._DOMAIN != bg_svp._DOMAIN
