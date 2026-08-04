"""Pins holdem/p2p/bg_svp.py -- BG single-value product argument (5.3).

Soundness focus: one happy path per shape, then rejection of every forgery
avenue -- wrong product, tampered commitments, tampered response scalars,
mismatched statement, mismatched key, and cross-invocation replay. A proof
that accepts any of these is worthless, so most tests are rejections.
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import ristretto as R
    from holdem.p2p import pedersen as P
    from holdem.p2p import bg_svp as svp
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


def _s(i: int) -> R.Scalar:
    return R.scalar_reduce(hashlib.sha512(f"svp:{i}".encode()).digest())


ZERO = R.Scalar(b"\x00" * 32)


def _setup(n, seed=b"svp-test"):
    ck = P.CommitmentKey.generate(n, seed=seed)
    a = [_s(i + 1) for i in range(n)]
    r = _s(1000)
    b = a[0]
    for ai in a[1:]:
        b = R.scalar_mul(b, ai)
    c_a = P.commit(ck, a, r)
    return ck, a, r, b, c_a


CONTEXT = b"poker-test|session=svp|hand=1|seat=0"


def _prove(ck, a, r, b, context=CONTEXT):
    return svp.prove(ck, a, r, b, context)


def _verify(ck, c_a, n, b, proof, context=CONTEXT):
    return svp.verify(ck, c_a, n, b, context, proof)


# --------------------------------------------------------------- happy path

@pytest.mark.parametrize("n", [2, 3, 4, 13])
def test_valid_proof_verifies(n):
    ck, a, r, b, c_a = _setup(n)
    proof = _prove(ck, a, r, b)
    assert _verify(ck, c_a, n, b, proof) is True


def test_proofs_are_randomised():
    ck, a, r, b, c_a = _setup(4)
    p1 = _prove(ck, a, r, b)
    p2 = _prove(ck, a, r, b)
    assert p1.c_d != p2.c_d
    assert _verify(ck, c_a, 4, b, p1)
    assert _verify(ck, c_a, 4, b, p2)


def test_zero_valued_entries_supported():
    """A vector containing zero has product zero; must still prove/verify."""
    ck = P.CommitmentKey.generate(4, seed=b"z")
    a = [_s(1), ZERO, _s(3), _s(4)]
    r = _s(9)
    c_a = P.commit(ck, a, r)
    proof = _prove(ck, a, r, ZERO)
    assert _verify(ck, c_a, 4, ZERO, proof) is True


# --------------------------------------------------------------- witness guard

def test_prove_rejects_bad_witness():
    ck, a, r, b, c_a = _setup(3)
    wrong_b = R.scalar_add(b, _s(7))
    with pytest.raises(ValueError):
        _prove(ck, a, r, wrong_b)


def test_prove_rejects_n_below_2():
    ck = P.CommitmentKey.generate(4, seed=b"n1")
    with pytest.raises(ValueError):
        _prove(ck, [_s(1)], _s(2), _s(1))


# --------------------------------------------------------------- soundness

def test_wrong_product_rejected():
    ck, a, r, b, c_a = _setup(4)
    proof = _prove(ck, a, r, b)
    wrong_b = R.scalar_add(b, _s(5))
    assert _verify(ck, c_a, 4, wrong_b, proof) is False


def test_wrong_commitment_rejected():
    ck, a, r, b, c_a = _setup(4)
    proof = _prove(ck, a, r, b)
    other_c = P.commit(ck, [_s(50 + i) for i in range(4)], _s(60))
    assert _verify(ck, other_c, 4, b, proof) is False


def test_wrong_key_rejected():
    """Challenge binds the commitment key: same proof under another key fails."""
    ck, a, r, b, c_a = _setup(4, seed=b"key-A")
    proof = _prove(ck, a, r, b)
    ck2 = P.CommitmentKey.generate(4, seed=b"key-B")
    assert _verify(ck2, c_a, 4, b, proof) is False


def test_transcript_binds_context():
    """The SVP challenge must change when its invocation context changes."""
    ck, a, r, b, c_a = _setup(4)
    proof = _prove(ck, a, r, b)
    x1 = svp._challenge(ck, c_a, 4, b, proof.c_d, proof.c_delta,
                         proof.c_Delta, CONTEXT)
    x2 = svp._challenge(ck, c_a, 4, b, proof.c_d, proof.c_delta,
                         proof.c_Delta, b"poker-test|session=other")
    assert x1 != x2


def test_cross_invocation_transfer_rejected():
    """A valid SVP proof cannot transfer to another invocation context."""
    ck, a, r, b, c_a = _setup(4)
    proof = _prove(ck, a, r, b, context=CONTEXT)
    other_context = b"poker-test|session=other|hand=1|seat=0"
    assert _verify(ck, c_a, 4, b, proof, context=other_context) is False


def test_tampered_c_d_rejected():
    ck, a, r, b, c_a = _setup(4)
    proof = _prove(ck, a, r, b)
    bad = svp.SVPProof(c_d=R.add(proof.c_d, R.G), c_delta=proof.c_delta,
                       c_Delta=proof.c_Delta, a_tilde=proof.a_tilde,
                       b_tilde=proof.b_tilde, r_tilde=proof.r_tilde,
                       s_tilde=proof.s_tilde)
    assert _verify(ck, c_a, 4, b, bad) is False


def test_tampered_c_delta_rejected():
    ck, a, r, b, c_a = _setup(4)
    proof = _prove(ck, a, r, b)
    bad = svp.SVPProof(c_d=proof.c_d, c_delta=R.add(proof.c_delta, R.G),
                       c_Delta=proof.c_Delta, a_tilde=proof.a_tilde,
                       b_tilde=proof.b_tilde, r_tilde=proof.r_tilde,
                       s_tilde=proof.s_tilde)
    assert _verify(ck, c_a, 4, b, bad) is False


def test_tampered_c_Delta_rejected():
    ck, a, r, b, c_a = _setup(4)
    proof = _prove(ck, a, r, b)
    bad = svp.SVPProof(c_d=proof.c_d, c_delta=proof.c_delta,
                       c_Delta=R.add(proof.c_Delta, R.G),
                       a_tilde=proof.a_tilde, b_tilde=proof.b_tilde,
                       r_tilde=proof.r_tilde, s_tilde=proof.s_tilde)
    assert _verify(ck, c_a, 4, b, bad) is False


def test_tampered_a_tilde_rejected():
    ck, a, r, b, c_a = _setup(4)
    proof = _prove(ck, a, r, b)
    at = list(proof.a_tilde)
    at[2] = R.scalar_add(at[2], _s(3))
    bad = svp.SVPProof(c_d=proof.c_d, c_delta=proof.c_delta,
                       c_Delta=proof.c_Delta, a_tilde=at,
                       b_tilde=proof.b_tilde, r_tilde=proof.r_tilde,
                       s_tilde=proof.s_tilde)
    assert _verify(ck, c_a, 4, b, bad) is False


def test_tampered_b_tilde_rejected():
    ck, a, r, b, c_a = _setup(4)
    proof = _prove(ck, a, r, b)
    bt = list(proof.b_tilde)
    bt[1] = R.scalar_add(bt[1], _s(3))
    bad = svp.SVPProof(c_d=proof.c_d, c_delta=proof.c_delta,
                       c_Delta=proof.c_Delta, a_tilde=proof.a_tilde,
                       b_tilde=bt, r_tilde=proof.r_tilde,
                       s_tilde=proof.s_tilde)
    assert _verify(ck, c_a, 4, b, bad) is False


def test_tampered_r_tilde_rejected():
    ck, a, r, b, c_a = _setup(4)
    proof = _prove(ck, a, r, b)
    bad = svp.SVPProof(c_d=proof.c_d, c_delta=proof.c_delta,
                       c_Delta=proof.c_Delta, a_tilde=proof.a_tilde,
                       b_tilde=proof.b_tilde,
                       r_tilde=R.scalar_add(proof.r_tilde, _s(1)),
                       s_tilde=proof.s_tilde)
    assert _verify(ck, c_a, 4, b, bad) is False


def test_tampered_s_tilde_rejected():
    ck, a, r, b, c_a = _setup(4)
    proof = _prove(ck, a, r, b)
    bad = svp.SVPProof(c_d=proof.c_d, c_delta=proof.c_delta,
                       c_Delta=proof.c_Delta, a_tilde=proof.a_tilde,
                       b_tilde=proof.b_tilde, r_tilde=proof.r_tilde,
                       s_tilde=R.scalar_add(proof.s_tilde, _s(1)))
    assert _verify(ck, c_a, 4, b, bad) is False


def test_wrong_length_vectors_rejected():
    ck, a, r, b, c_a = _setup(4)
    proof = _prove(ck, a, r, b)
    bad = svp.SVPProof(c_d=proof.c_d, c_delta=proof.c_delta,
                       c_Delta=proof.c_Delta, a_tilde=proof.a_tilde[:3],
                       b_tilde=proof.b_tilde, r_tilde=proof.r_tilde,
                       s_tilde=proof.s_tilde)
    assert _verify(ck, c_a, 4, b, bad) is False
    assert _verify(ck, c_a, 3, b, proof) is False   # n mismatch vs proof


if __name__ == "__main__":
    passed = total = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        marks = getattr(fn, "pytestmark", [])
        params = None
        for m in marks:
            if m.name == "parametrize":
                params = m.args[1]
        cases = params if params else [None]
        for c in cases:
            total += 1
            try:
                fn(c) if params else fn()
                passed += 1
                print(f"  {name}{'['+str(c)+']' if params else ''}: ok")
            except Exception as exc:
                print(f"  {name}{'['+str(c)+']' if params else ''}: FAIL - {exc}")
    print(f"{passed}/{total} passed")
