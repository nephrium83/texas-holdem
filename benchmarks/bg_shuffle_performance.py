"""Benchmark the standalone Bayer-Groth shuffle prevention proof.

This measures one real 52-card shuffle proof.  A multi-seat hand performs
one such proof per shuffler, so the report also includes serial-chain
estimates for the supported 2-, 4-, and 9-seat configurations.

Reports include the source checkout revision and clean/dirty/unknown state,
independent of the caller's directory. For a Git-less source archive, pass
--source-revision <full-commit-hash>; it is labelled declared, never verified.
Unknown or dirty provenance must not be represented as clean release evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import re
import statistics
import subprocess
import time
from pathlib import Path

from holdem.p2p import bg_shuffle, elgamal, ristretto as R
from holdem.p2p.pedersen import CommitmentKey
from holdem.p2p.shuffle_mp import random_permutation


ROOT = Path(__file__).resolve().parents[1]


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
            "p95": round(sorted(values)[math.ceil(len(values) * .95) - 1], 3),
            "max": round(max(values), 3),
        }

    return {
        "samples": len(records),
        "prove_ms": metric("prove_ms"),
        "verify_ms": metric("verify_ms"),
        "proof_bytes": metric("proof_bytes"),
    }


def _source_metadata(declared_revision: str | None = None) -> dict:
    """Describe this source tree, never the caller's current directory.

    An archive may declare its originating commit, but that is labelled as
    supplied provenance, not a verified clean Git checkout.
    """
    def git(*args):
        result = subprocess.run(["git", *args], cwd=ROOT,
                                capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise ValueError("source Git metadata unavailable")
        return result.stdout.strip()

    try:
        # A source archive nested inside some unrelated repository must not
        # inherit that outer repository's commit or cleanliness.
        if Path(git("rev-parse", "--show-toplevel")).resolve() != ROOT:
            raise ValueError("source is not the Git worktree root")
        commit = git("rev-parse", "HEAD")
        dirty = bool(git("status", "--porcelain", "--untracked-files=normal"))
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"commit": declared_revision or "unknown",
                "source_state": "unknown",
                "revision_source": "declared" if declared_revision else "unavailable"}
    if declared_revision and declared_revision != commit:
        raise ValueError("--source-revision disagrees with this source checkout")
    return {"commit": commit, "source_state": "dirty" if dirty else "clean",
            "revision_source": "git"}


def _revision(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
        raise argparse.ArgumentTypeError("source revision must be a full Git commit hash")
    return value.lower()


def main() -> None:
    from holdem import native_crypto
    native_crypto.initialize()
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--m", type=int, default=4)
    parser.add_argument("--n", type=int, default=13)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-revision", type=_revision,
                        help="originating full commit hash for Git-less source distributions; labelled declared")
    args = parser.parse_args()
    if args.samples < 1 or args.m * args.n != 52:
        raise SystemExit("use positive samples and dimensions with m*n == 52")

    try:
        source_metadata = _source_metadata(args.source_revision)
    except ValueError as exc:
        parser.error(str(exc))
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
            **source_metadata,
            "quantile_method": "nearest rank: ceil(p * sample_count)",
            "proof": "bayer-groth shuffle prevention",
        },
        "summary": summary,
        "seat_chain_estimates": {
            str(seats): {
                "serial_prove_plus_verify_ms": round(seats * (prove + verify), 3),
                "serial_prove_plus_all_verifiers_ms": round(
                    seats * (prove + seats * verify), 3),
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
