"""Compare the two shuffle-prevention constructions on one harness.

docs/BG_SHUFFLE_BENCHMARK.md asks for three arms: detection-only,
Bayer-Groth prevention, and the existing cut-and-choose path.
mental_deal_startup.py measures the first two end to end. The third cannot
be measured that way because shuffle_proof.py was never wired into the
coordinator, and wiring an inferior construction into the live protocol to
benchmark it would be the wrong trade.

Instead this measures both proof systems on the same 52-card shuffle and
projects each to a full hand with the seat-chain model:

    proof work = seats * prove + seats^2 * verify

one proof per shuffler, verified by every peer. The model is not assumed --
``--validate`` checks it against the measured Bayer-Groth figures from the
integrated run, so the cut-and-choose projection rests on a model shown to
hold for the arm we can measure directly.

Security note: DEFAULT_K = 128 is a full security parameter for the
cut-and-choose path, not a statistical one (see shuffle_proof.py). It is
not reduced here to make the comparison flattering.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import time
from pathlib import Path

from holdem.p2p import bg_shuffle, elgamal, ristretto as R, shuffle_proof
from holdem.p2p.mental_deal import BG_M, BG_N, bg_commitment_key
from holdem.p2p.shuffle_mp import random_permutation

# Measured Bayer-Groth proof work from the integrated run, used to check
# the seat-chain model before it is applied to cut-and-choose.
# See docs/BG_SHUFFLE_BENCHMARK.md.
MEASURED_BG = {
    2: {"prove_ms": 259.5, "verify_ms": 113.6},
    4: {"prove_ms": 516.3, "verify_ms": 450.7},
    9: {"prove_ms": 1161.7, "verify_ms": 2284.2},
}


def _statement(sample: int):
    pk = R.mul_base(R.scalar_reduce(
        hashlib.sha512(f"cmp:pk:{sample}".encode()).digest()))
    in_deck = elgamal.make_trivial_deck()
    perm = random_permutation(len(in_deck))
    scalars = [R.random_scalar() for _ in range(len(in_deck))]
    out_deck = [elgamal.reencrypt(pk, in_deck[src], scalars[i])
                for i, src in enumerate(perm)]
    return pk, in_deck, out_deck, perm, scalars


def _time_bayer_groth(sample: int) -> dict:
    pk, in_deck, out_deck, perm, scalars = _statement(sample)
    ck = bg_commitment_key()
    ctx = f"comparison|{sample}".encode()

    started = time.perf_counter()
    proof = bg_shuffle.prove(pk, ck, in_deck, out_deck, perm, scalars,
                             BG_M, BG_N, ctx)
    prove_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    ok = bg_shuffle.verify(pk, ck, in_deck, out_deck, BG_M, BG_N, ctx, proof)
    verify_ms = (time.perf_counter() - started) * 1000
    if not ok:
        raise RuntimeError("bayer-groth proof failed verification")
    return {"prove_ms": prove_ms, "verify_ms": verify_ms}


def _time_cut_and_choose(sample: int, k: int) -> dict:
    pk, in_deck, out_deck, perm, scalars = _statement(sample)
    ctx = f"comparison|{sample}".encode()

    started = time.perf_counter()
    proof = shuffle_proof.prove(pk, in_deck, out_deck, perm, scalars, ctx, k)
    prove_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    ok = shuffle_proof.verify(pk, in_deck, out_deck, proof, ctx, k)
    verify_ms = (time.perf_counter() - started) * 1000
    if not ok:
        raise RuntimeError("cut-and-choose proof failed verification")

    # k shadow decks of 52 ciphertexts, plus a permutation index and a
    # scalar per position for each opening.
    n = len(in_deck)
    proof_bytes = k * n * 64 + k * n * (4 + 32)
    return {"prove_ms": prove_ms, "verify_ms": verify_ms,
            "proof_bytes": proof_bytes}


def _p50(values):
    return round(statistics.median(values), 3)


def _chain(prove_ms: float, verify_ms: float, seats: int) -> dict:
    """One proof per shuffler; every peer verifies every round."""
    return {
        "prove_ms": round(seats * prove_ms, 1),
        "verify_ms": round(seats * seats * verify_ms, 1),
        "total_proof_work_ms": round(
            seats * prove_ms + seats * seats * verify_ms, 1),
    }


def _validate_model(bg: dict) -> list:
    """Check the seat-chain model against the measured integrated run."""
    rows = []
    for seats, measured in MEASURED_BG.items():
        predicted = _chain(bg["prove_ms"], bg["verify_ms"], seats)
        for field in ("prove_ms", "verify_ms"):
            actual = measured[field]
            error = (predicted[field] - actual) / actual * 100
            rows.append({
                "seats": seats,
                "metric": field,
                "predicted_ms": predicted[field],
                "measured_ms": actual,
                "error_pct": round(error, 1),
            })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--k", type=int, default=shuffle_proof.DEFAULT_K,
                        help="cut-and-choose security parameter (do not lower)")
    parser.add_argument("--seats", type=int, nargs="+", default=[2, 4, 9])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.k < shuffle_proof.DEFAULT_K:
        raise SystemExit(
            f"--k below DEFAULT_K={shuffle_proof.DEFAULT_K} would weaken the "
            f"security parameter to flatter the comparison; refusing")

    bg_runs = [_time_bayer_groth(i) for i in range(args.samples)]
    cc_runs = [_time_cut_and_choose(i, args.k) for i in range(args.samples)]
    bg = {"prove_ms": _p50([r["prove_ms"] for r in bg_runs]),
          "verify_ms": _p50([r["verify_ms"] for r in bg_runs])}
    cc = {"prove_ms": _p50([r["prove_ms"] for r in cc_runs]),
          "verify_ms": _p50([r["verify_ms"] for r in cc_runs]),
          "proof_bytes": cc_runs[0]["proof_bytes"]}

    report = {
        "metadata": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "libsodium": R.libsodium_version(),
            "samples": args.samples,
            "cut_and_choose_k": args.k,
            "bg_layout": f"{BG_M}x{BG_N}",
        },
        "per_round": {"bayer_groth": bg, "cut_and_choose": cc},
        "model_validation": _validate_model(bg),
        "projected_proof_work": {
            str(seats): {
                "bayer_groth": _chain(bg["prove_ms"], bg["verify_ms"], seats),
                "cut_and_choose": _chain(cc["prove_ms"], cc["verify_ms"],
                                         seats),
            }
            for seats in args.seats
        },
    }
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
