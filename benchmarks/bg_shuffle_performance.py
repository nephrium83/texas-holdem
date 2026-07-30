"""Benchmark the standalone Bayer-Groth shuffle prevention proof.

This measures one real 52-card shuffle proof.  A multi-seat hand performs
one such proof per shuffler, so the report also includes serial-chain
estimates for the supported 2-, 4-, and 9-seat configurations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import time
from pathlib import Path

from holdem.p2p import bg_shuffle, elgamal, ristretto as R
from holdem.p2p.pedersen import CommitmentKey
from holdem.p2p.shuffle_mp import random_permutation


def _scalar(label: str) -> R.Scalar:
    return R.scalar_reduce(hashlib.sha512(label.encode()).digest())


def _proof_size(proof: bg_shuffle.ShuffleProof) -> int:
    """Count canonical point/scalar/cipher payload bytes, excluding framing."""
    points = len(proof.a_commits) + len(proof.b_commits)
    product = proof.product
    points += 1 + len(product.hadamard.c_B_interior)
    points += 2 + len(product.hadamard.zero.c_D)
    scalars = (len(product.hadamard.zero.a_tilde) +
               len(product.hadamard.zero.b_tilde) + 3)

    multi = proof.multi
    points += 1 + len(multi.commit_b_k)
    ciphers = len(multi.vector_e_k)
    scalars += 4 + len(multi.a_blinded)
    return points * 32 + scalars * 32 + ciphers * 64


def _sample(m: int, n: int, sample: int) -> dict:
    ck = CommitmentKey.generate(n, seed=b"bg-shuffle-benchmark-v1")
    pk = R.mul_base(_scalar(f"pk:{sample}"))
    in_deck = elgamal.make_trivial_deck()
    perm = random_permutation(len(in_deck))
    scalars = [R.scalar_reduce(hashlib.sha512(
        f"rho:{sample}:{i}".encode()).digest())
               for i in range(len(in_deck))]
    out_deck = [elgamal.reencrypt(pk, in_deck[source], scalars[i])
                for i, source in enumerate(perm)]

    started = time.perf_counter()
    proof = bg_shuffle.prove(
        pk, ck, in_deck, out_deck, perm, scalars, m, n,
        f"benchmark|sample={sample}".encode())
    prove_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    verified = bg_shuffle.verify(
        pk, ck, in_deck, out_deck, m, n,
        f"benchmark|sample={sample}".encode(), proof)
    verify_ms = (time.perf_counter() - started) * 1000
    if not verified:
        raise RuntimeError("benchmark proof failed verification")

    return {
        "sample": sample,
        "prove_ms": round(prove_ms, 3),
        "verify_ms": round(verify_ms, 3),
        "proof_bytes": _proof_size(proof),
    }


def _summary(records: list[dict]) -> dict:
    def metric(name: str) -> dict:
        values = [record[name] for record in records]
        return {
            "mean": round(statistics.mean(values), 3),
            "p50": round(statistics.median(values), 3),
            "p95": round(sorted(values)[max(0, int(len(values) * .95) - 1)], 3),
            "max": round(max(values), 3),
        }

    return {
        "samples": len(records),
        "prove_ms": metric("prove_ms"),
        "verify_ms": metric("verify_ms"),
        "proof_bytes": metric("proof_bytes"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--m", type=int, default=4)
    parser.add_argument("--n", type=int, default=13)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.samples < 1 or args.m * args.n != 52:
        raise SystemExit("use positive samples and dimensions with m*n == 52")

    records = [_sample(args.m, args.n, sample)
               for sample in range(args.samples)]
    summary = _summary(records)
    prove = summary["prove_ms"]["p50"]
    verify = summary["verify_ms"]["p50"]
    report = {
        "configuration": {
            "cards": 52,
            "m": args.m,
            "n": args.n,
            "samples": args.samples,
        },
        "metadata": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "commit": "working-tree",
            "proof": "bayer-groth shuffle prevention",
        },
        "summary": summary,
        "seat_chain_estimates": {
            str(seats): {
                "serial_prove_plus_verify_ms": round(seats * (prove + verify), 3),
                "serial_prove_plus_all_verifiers_ms": round(
                    seats * (prove + (seats - 1) * verify), 3),
            }
            for seats in (2, 4, 9)
        },
        "records": records,
    }
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
