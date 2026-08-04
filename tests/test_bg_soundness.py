"""Soundness tests for the Bayer-Groth sub-arguments.

Each module's own test file covers completeness and tamper-resistance.
Neither models the threat that matters: a prover that runs the honest
algorithm over a witness it knows to be false. Prover-side checks catch
honest bugs, but an attacker deletes them first, so soundness is entirely
the verifier's job.

Every test here becomes that attacker by patching out
``bg_witness.require_witness`` on the modules under test, including nested
dependencies -- otherwise an inner prover's self-check masks whatever the
outer verifier does or does not do.

This is not a hypothetical exercise. The shuffle argument one layer up
shipped unsound (the verifier read its multi-exponentiation statement out
of the proof instead of recomputing it from the public input deck) and
every test in the suite passed. See test_bg_shuffle_soundness.py.
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import bg_hadamard, bg_product, bg_svp, bg_witness
    from holdem.p2p import bg_zero
    from holdem.p2p import ristretto as R
    from holdem.p2p.pedersen import CommitmentKey, commit
    from holdem.p2p.ristretto import Scalar
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


ONE = Scalar(b"\x01" + b"\x00" * 31)
ZERO = Scalar(b"\x00" * 32)
CTX = b"soundness-context"
M, N = 3, 4


def _s(i: int) -> Scalar:
    return R.scalar_reduce(hashlib.sha512(f"bgsound:{i}".encode()).digest())


def _product(vec):
    out = ONE
    for value in vec:
        out = R.scalar_mul(out, value)
    return out


@pytest.fixture
def ck():
    return CommitmentKey.generate(N, seed=b"bg-soundness")


@pytest.fixture
def cheat(monkeypatch):
    """Turn the named modules' provers into ones that never self-check."""
    def apply(*modules):
        for module in modules:
            monkeypatch.setattr(module, "require_witness",
                                lambda ok, message: None)
    return apply


@pytest.fixture
def matrix(ck):
    rows = [[_s(100 + i * 10 + j) for j in range(N)] for i in range(M)]
    blinders = [_s(200 + i) for i in range(M)]
    commits = [commit(ck, rows[i], blinders[i]) for i in range(M)]
    return rows, blinders, commits


# --------------------------------------------------------------- the seam

def test_require_witness_raises_by_default():
    """The seam must be a real check in production, not a no-op."""
    bg_witness.require_witness(True, "fine")
    with pytest.raises(ValueError, match="bad witness"):
        bg_witness.require_witness(False, "bad witness")


@pytest.mark.parametrize("module", [bg_zero, bg_hadamard, bg_product, bg_svp])
def test_each_prover_is_wired_to_the_seam(module):
    """If a module stopped routing through require_witness, its forgery
    test below would silently start proving nothing."""
    assert module.require_witness is bg_witness.require_witness


# --------------------------------------------------------------- bg_zero

def _zero_case(ck, a_rows, b_rows):
    r = [_s(400 + i) for i in range(M)]
    s = [_s(500 + i) for i in range(M)]
    c_A = [commit(ck, a_rows[i], r[i]) for i in range(M)]
    c_B = [commit(ck, b_rows[i], s[i]) for i in range(M)]
    bmap = bg_zero.BilinearMap.from_challenge(_s(7), N)
    proof = bg_zero.prove(ck, c_A, a_rows, r, c_B, b_rows, s, bmap, CTX)
    return bg_zero.verify(ck, c_A, c_B, bmap, CTX, proof)


def test_zero_argument_accepts_a_satisfying_witness(ck, cheat):
    """Control: every b_i zero makes the bilinear sum genuinely vanish."""
    cheat(bg_zero)
    a_rows = [[_s(600 + i * 10 + j) for j in range(N)] for i in range(M)]
    assert _zero_case(ck, a_rows, [[ZERO] * N for _ in range(M)]) is True


def test_zero_argument_rejects_a_nonzero_relation(ck, cheat):
    cheat(bg_zero)
    a_rows = [[_s(600 + i * 10 + j) for j in range(N)] for i in range(M)]
    assert _zero_case(ck, a_rows, [[ONE] * N for _ in range(M)]) is False


def test_zero_argument_rejects_bad_witness_when_seam_intact(ck):
    """With the seam in place the prover refuses outright."""
    a_rows = [[_s(600 + i * 10 + j) for j in range(N)] for i in range(M)]
    with pytest.raises(ValueError, match="witness"):
        _zero_case(ck, a_rows, [[ONE] * N for _ in range(M)])


# ----------------------------------------------------------- bg_hadamard

def _hadamard_case(ck, matrix, b_vec):
    rows, blinders, commits = matrix
    s = _s(300)
    c_b = commit(ck, b_vec, s)
    proof = bg_hadamard.prove(ck, commits, rows, blinders, c_b, b_vec, s, CTX)
    return bg_hadamard.verify(ck, commits, c_b, N, CTX, proof)


def _hadamard_truth(matrix):
    rows, _, _ = matrix
    return [_product([rows[i][j] for i in range(M)]) for j in range(N)]


def test_hadamard_accepts_the_true_product(ck, matrix, cheat):
    cheat(bg_hadamard, bg_zero)
    assert _hadamard_case(ck, matrix, _hadamard_truth(matrix)) is True


def test_hadamard_rejects_a_wrong_product(ck, matrix, cheat):
    """One coordinate off by one is still a lie."""
    cheat(bg_hadamard, bg_zero)
    forged = list(_hadamard_truth(matrix))
    forged[0] = R.scalar_add(forged[0], ONE)
    assert _hadamard_case(ck, matrix, forged) is False


def test_hadamard_rejects_an_unrelated_product(ck, matrix, cheat):
    cheat(bg_hadamard, bg_zero)
    assert _hadamard_case(ck, matrix, [_s(900 + j) for j in range(N)]) is False


# ------------------------------------------------------------ bg_product

def _product_case(ck, matrix, claim):
    rows, blinders, commits = matrix
    proof = bg_product.prove(ck, commits, rows, blinders, claim, CTX)
    return bg_product.verify(ck, commits, N, claim, CTX, proof)


def _product_truth(matrix):
    rows, _, _ = matrix
    return _product([value for row in rows for value in row])


def test_product_accepts_the_true_claim(ck, matrix, cheat):
    cheat(bg_product, bg_hadamard, bg_zero, bg_svp)
    assert _product_case(ck, matrix, _product_truth(matrix)) is True


def test_product_rejects_a_wrong_claim(ck, matrix, cheat):
    cheat(bg_product, bg_hadamard, bg_zero, bg_svp)
    forged = R.scalar_add(_product_truth(matrix), ONE)
    assert _product_case(ck, matrix, forged) is False


def test_product_rejects_a_zero_claim(ck, matrix, cheat):
    """Zero is the claim an attacker reaches for when a factor is unknown."""
    cheat(bg_product, bg_hadamard, bg_zero, bg_svp)
    assert _product_case(ck, matrix, ZERO) is False


# ---------------------------------------------------------------- bg_svp

def _svp_case(ck, a_vec, claim):
    r = _s(99)
    proof = bg_svp.prove(ck, a_vec, r, claim, CTX)
    return bg_svp.verify(ck, commit(ck, a_vec, r), N, claim, CTX, proof)


def test_svp_accepts_the_true_product(ck, cheat):
    cheat(bg_svp)
    a_vec = [_s(10 + i) for i in range(N)]
    assert _svp_case(ck, a_vec, _product(a_vec)) is True


def test_svp_rejects_a_wrong_product(ck, cheat):
    cheat(bg_svp)
    a_vec = [_s(10 + i) for i in range(N)]
    assert _svp_case(ck, a_vec, R.scalar_add(_product(a_vec), ONE)) is False


def test_svp_rejects_a_zero_product(ck, cheat):
    cheat(bg_svp)
    a_vec = [_s(10 + i) for i in range(N)]
    assert _svp_case(ck, a_vec, ZERO) is False
