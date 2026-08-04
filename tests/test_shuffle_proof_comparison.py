"""Pins the logic in benchmarks/shuffle_proof_comparison.py.

A benchmark whose numbers land in the docs is load-bearing: the seat-chain
model it projects with, and its refusal to weaken the cut-and-choose
security parameter, are both worth locking down. The timing itself is not
tested -- that is what the benchmark measures.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from holdem.p2p import shuffle_proof
except RuntimeError as exc:
    pytest.skip(f"libsodium unavailable: {exc}", allow_module_level=True)


def _load():
    path = ROOT / "benchmarks" / "shuffle_proof_comparison.py"
    spec = importlib.util.spec_from_file_location("shuffle_cmp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cmp_mod = _load()


# ------------------------------------------------------- seat-chain model

def test_chain_counts_one_proof_per_shuffler():
    """seats proofs, not seats squared -- proving scales linearly."""
    assert cmp_mod._chain(100.0, 10.0, 4)["prove_ms"] == 400.0


def test_chain_counts_every_peer_verifying_every_round():
    """seats squared verifications: each of seats rounds checked by each of
    seats peers. Getting this wrong understates cost at a full table, which
    is exactly how the old 20 s figure went astray."""
    assert cmp_mod._chain(100.0, 10.0, 4)["verify_ms"] == 160.0
    assert cmp_mod._chain(100.0, 10.0, 9)["verify_ms"] == 810.0


def test_chain_total_is_the_sum():
    chain = cmp_mod._chain(100.0, 10.0, 9)
    assert chain["total_proof_work_ms"] == pytest.approx(
        chain["prove_ms"] + chain["verify_ms"])


def test_verification_dominates_at_a_full_table():
    """The finding the optimization order rests on."""
    chain = cmp_mod._chain(137.6, 34.6, 9)
    assert chain["verify_ms"] > chain["prove_ms"]


def test_chain_scaling_is_quadratic_in_seats_for_verification():
    small = cmp_mod._chain(1.0, 1.0, 2)["verify_ms"]
    large = cmp_mod._chain(1.0, 1.0, 8)["verify_ms"]
    assert large == small * 16          # (8/2)^2


# ----------------------------------------------------- model validation

def test_model_validation_compares_against_measured_figures():
    rows = cmp_mod._validate_model({"prove_ms": 137.6, "verify_ms": 34.6})
    assert {row["seats"] for row in rows} == set(cmp_mod.MEASURED_BG)
    for row in rows:
        assert row["measured_ms"] == \
            cmp_mod.MEASURED_BG[row["seats"]][row["metric"]]


def test_model_error_stays_within_a_conservative_band():
    """The projection is only defensible if the model tracks the arm we can
    measure. It should overpredict -- a conservative bound -- and not by a
    wild margin."""
    rows = cmp_mod._validate_model({"prove_ms": 137.6, "verify_ms": 34.6})
    for row in rows:
        assert 0 <= row["error_pct"] < 35, row


# --------------------------------------------------- security parameter

def test_benchmark_refuses_to_weaken_k(monkeypatch, capsys):
    """k is a full security parameter for the cut-and-choose path, not a
    statistical one. Lowering it to make the comparison flattering must be
    refused rather than quietly honoured."""
    monkeypatch.setattr(
        sys, "argv",
        ["shuffle_proof_comparison.py", "--k",
         str(shuffle_proof.DEFAULT_K - 1)])
    with pytest.raises(SystemExit) as exc:
        cmp_mod.main()
    assert "security parameter" in str(exc.value)


def test_default_k_matches_the_module_default():
    assert cmp_mod.shuffle_proof.DEFAULT_K == shuffle_proof.DEFAULT_K == 128
