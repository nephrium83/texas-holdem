"""Tests for the Bayer-Groth section 5.2 product composition."""
import hashlib
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import bg_product as PARG
    from holdem.p2p import pedersen as P
    from holdem.p2p import ristretto as R
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


ZERO = R.Scalar(b"\x00" * 32)
CTX = b"session=1|hand=7|shuffle=0"


def _s(i: int) -> R.Scalar:
    return R.scalar_reduce(hashlib.sha512(f"product:{i}".encode()).digest())


def _bump(p: R.Point) -> R.Point:
    return R.add(p, R.mul_base(_s(999)))


def _setup(m=3, n=3, seed=b"product-test", context=CTX):
    ck = P.CommitmentKey.generate(max(m, n), seed=seed)
    a = [[_s(100 * i + j) for j in range(n)] for i in range(m)]
    r = [_s(1000 + i) for i in range(m)]
    c_A = [P.commit(ck, a[i], r[i]) for i in range(m)]
    b = R.Scalar(bytes([1]) + bytes(31))
    for vector in a:
        for element in vector:
            b = R.scalar_mul(b, element)
    return ck, a, r, c_A, b, context


def _prove(setup):
    ck, a, r, c_A, b, context = setup
    return PARG.prove(ck, c_A, a, r, b, context)


def test_valid_product_proof_verifies():
    setup = _setup()
    assert PARG.verify(setup[0], setup[3], 3, setup[4], setup[5],
                       _prove(setup))


@pytest.mark.parametrize("m,n", [(2, 2), (2, 4), (4, 2), (5, 3)])
def test_valid_shapes_verify(m, n):
    setup = _setup(m, n)
    assert PARG.verify(setup[0], setup[3], n, setup[4], setup[5],
                       _prove(setup))


def test_proof_contains_one_shared_internal_commitment():
    proof = _prove(_setup())
    assert isinstance(proof.c_b, R.Point)
    assert proof.hadamard is not None
    assert proof.svp is not None


def test_wrong_public_product_rejected_by_prover():
    setup = _setup()
    wrong = R.scalar_add(setup[4], _s(700))
    with pytest.raises(ValueError, match="product of all matrix entries"):
        PARG.prove(setup[0], setup[3], setup[1], setup[2], wrong, setup[5])


@pytest.mark.parametrize("m,n", [(1, 2), (3, 1)])
def test_dimensions_below_theorem_8_bounds_rejected(m, n):
    setup = _setup(max(m, 2), n)
    a = setup[1][:m]
    r = setup[2][:m]
    c_A = setup[3][:m]
    with pytest.raises(ValueError):
        PARG.prove(setup[0], c_A, a, r, setup[4], setup[5])


def test_changed_public_statement_rejected():
    setup = _setup()
    proof = _prove(setup)
    assert not PARG.verify(setup[0], [_bump(setup[3][0]), *setup[3][1:]],
                           3, setup[4], setup[5], proof)
    assert not PARG.verify(setup[0], setup[3], 3, _s(888), setup[5], proof)
    assert not PARG.verify(setup[0], setup[3], 2, setup[4], setup[5], proof)


def test_changed_context_rejected():
    setup = _setup()
    proof = _prove(setup)
    assert not PARG.verify(setup[0], setup[3], 3, setup[4], b"other", proof)


def test_changed_internal_commitment_rejected():
    setup = _setup()
    proof = _prove(setup)
    changed = replace(proof, c_b=_bump(proof.c_b))
    assert not PARG.verify(setup[0], setup[3], 3, setup[4], setup[5], changed)


def test_proof_does_not_transfer_between_commitment_keys():
    setup = _setup()
    proof = _prove(setup)
    other = P.CommitmentKey.generate(3, seed=b"other")
    assert not PARG.verify(other, setup[3], 3, setup[4], setup[5], proof)


def test_ragged_matrix_rejected():
    setup = _setup()
    a = [list(v) for v in setup[1]]
    a[1].pop()
    with pytest.raises(ValueError, match="width n"):
        PARG.prove(setup[0], setup[3], a, setup[2], setup[4], setup[5])


def test_opening_mismatch_rejected():
    setup = _setup()
    r = list(setup[2])
    r[0] = _s(12345)
    with pytest.raises(ValueError, match="does not open"):
        PARG.prove(setup[0], setup[3], setup[1], r, setup[4], setup[5])
