"""Benchmark the complete mental-poker hand-start path.

Unlike ``bg_shuffle_performance.py``, which times one proof in isolation,
this drives a whole peer-symmetric hand through the MentalDeal
coordinator -- distributed key ceremony, the full shuffle chain, and the
selective deal down to recovered hole cards -- in both detection-only and
Bayer-Groth prevention modes. That is the number docs/PERFORMANCE_BUDGET.md
actually gates on, and the comparison isolates what prevention costs.

Every seat is simulated in-process and every message is delivered to every
seat (including its sender), so the proof-verification column reflects the
real n-verifiers-per-round load rather than a single check. Transport is
excluded by construction: this measures crypto and orchestration only, and
the reported byte counts are what a real transport would have to carry.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import time
from pathlib import Path

from holdem.p2p import bg_shuffle, bg_wire, ristretto as R
from holdem.p2p import mental_deal as md
from holdem.p2p.mental_deal import MentalDeal, Phase


class _Probe:
    """Accumulates call count and wall time for one wrapped function."""

    def __init__(self) -> None:
        self.calls = 0
        self.seconds = 0.0

    def wrap(self, fn):
        def wrapped(*args, **kwargs):
            started = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                self.seconds += time.perf_counter() - started
                self.calls += 1
        return wrapped

    @property
    def ms(self) -> float:
        return self.seconds * 1000


class _Instrumented:
    """Times prove/verify/encode/decode for the duration of one hand.

    mental_deal calls these through its imported module objects, so
    patching the attribute on the module is enough to observe every call
    the coordinator makes without threading hooks through the protocol.
    """

    def __enter__(self):
        self.prove = _Probe()
        self.verify = _Probe()
        self.encode = _Probe()
        self.decode = _Probe()
        self._originals = (bg_shuffle.prove, bg_shuffle.verify,
                           bg_wire.encode, bg_wire.decode)
        bg_shuffle.prove = self.prove.wrap(self._originals[0])
        bg_shuffle.verify = self.verify.wrap(self._originals[1])
        bg_wire.encode = self.encode.wrap(self._originals[2])
        bg_wire.decode = self.decode.wrap(self._originals[3])
        return self

    def __exit__(self, *exc):
        (bg_shuffle.prove, bg_shuffle.verify,
         bg_wire.encode, bg_wire.decode) = self._originals
        return False


def _run_hand(seats: list[int], prevention: bool, hand_no: int) -> dict:
    """Drive one full hand to recovered hole cards; return its metrics."""
    deals = {
        s: MentalDeal(session_id="benchmark", hand_no=hand_no, seat=s,
                      seats_in=list(seats), button=0,
                      master_secret=f"seat-{s}-secret".encode(),
                      prevention=prevention)
        for s in seats
    }
    marks: dict[str, float] = {}
    wire_bytes = 0
    proof_bytes = 0

    with _Instrumented() as probes:
        started = time.perf_counter()
        queue: list[dict] = []
        for s in seats:
            queue.extend(deals[s].start())

        while queue:
            msg = queue.pop(0)
            encoded = json.dumps(msg)
            wire_bytes += len(encoded)
            if "proof" in msg:
                proof_bytes += len(json.dumps(msg["proof"]))
            for s in seats:
                queue.extend(deals[s].handle(dict(msg)))

            now = time.perf_counter()
            if "keygen" not in marks and \
                    all(deals[s].is_done_with_keygen() for s in seats):
                marks["keygen"] = now - started
            if "shuffle" not in marks and \
                    all(deals[s].is_shuffle_complete() for s in seats):
                marks["shuffle"] = now - started
            if "deal" not in marks and \
                    all(deals[s].hole_complete() for s in seats):
                marks["deal"] = now - started
        total = time.perf_counter() - started

    for s in seats:
        if deals[s].phase != Phase.DEAL or deals[s].abort_reason:
            raise RuntimeError(
                f"seat {s} did not reach the deal: {deals[s].abort_reason}")
        if not deals[s].hole_complete():
            raise RuntimeError(f"seat {s} never recovered its hole cards")

    keygen = marks.get("keygen", 0.0)
    shuffle = marks.get("shuffle", keygen)
    deal = marks.get("deal", shuffle)
    return {
        "hand": hand_no,
        "total_ms": round(total * 1000, 3),
        "keygen_ms": round(keygen * 1000, 3),
        "shuffle_ms": round((shuffle - keygen) * 1000, 3),
        "deal_ms": round((deal - shuffle) * 1000, 3),
        "prove_ms": round(probes.prove.ms, 3),
        "prove_calls": probes.prove.calls,
        "verify_ms": round(probes.verify.ms, 3),
        "verify_calls": probes.verify.calls,
        "serialize_ms": round(probes.encode.ms + probes.decode.ms, 3),
        "wire_bytes": wire_bytes,
        "proof_bytes": proof_bytes,
    }


_METRICS = ("total_ms", "keygen_ms", "shuffle_ms", "deal_ms", "prove_ms",
            "verify_ms", "serialize_ms", "wire_bytes", "proof_bytes")


def _summary(records: list[dict]) -> dict:
    def metric(name: str) -> dict:
        values = sorted(record[name] for record in records)
        return {
            "mean": round(statistics.mean(values), 3),
            "p50": round(statistics.median(values), 3),
            "p95": round(values[max(0, int(len(values) * .95) - 1)], 3),
            "max": round(max(values), 3),
        }

    return {name: metric(name) for name in _METRICS}


def _configuration(count: int, prevention: bool, hands: int) -> dict:
    seats = list(range(count))
    # The first hand is the cold run: NUMS generator derivation, the
    # commitment-key cache, and libsodium's own warmup all land on it.
    cold = _run_hand(seats, prevention, hand_no=1)
    warm = [_run_hand(seats, prevention, hand_no=h)
            for h in range(2, hands + 1)]
    return {
        "seats": count,
        "prevention": prevention,
        "hands": hands,
        "cold": cold,
        "warm_summary": _summary(warm),
        "warm_records": warm,
    }


def _commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _metadata() -> dict:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor() or "unknown",
        "cpu_count": os.cpu_count(),
        "libsodium": R.libsodium_version(),
        "commit": _commit(),
        "m": md.BG_M,
        "n": md.BG_N,
        "proof": "bayer-groth shuffle prevention",
        "measures": "in-process coordinator; excludes transport",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hands", type=int, default=20,
                        help="hands per configuration, including the cold one")
    parser.add_argument("--seats", type=int, nargs="+", default=[2, 4, 9])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.hands < 2:
        raise SystemExit("use --hands 2 or more (one cold plus warm runs)")
    if any(count < 2 or count > 9 for count in args.seats):
        raise SystemExit("seat counts must be between 2 and 9")

    configurations = [
        _configuration(count, prevention, args.hands)
        for count in args.seats
        for prevention in (False, True)
    ]
    report = {
        "metadata": _metadata(),
        "configurations": configurations,
        "comparison": [
            {
                "seats": count,
                "detection_p50_ms": detection,
                "prevention_p50_ms": prevention,
                "added_ms": round(prevention - detection, 3),
            }
            for count, detection, prevention in (
                (
                    count,
                    next(c["warm_summary"]["total_ms"]["p50"]
                         for c in configurations
                         if c["seats"] == count and not c["prevention"]),
                    next(c["warm_summary"]["total_ms"]["p50"]
                         for c in configurations
                         if c["seats"] == count and c["prevention"]),
                )
                for count in args.seats
            )
        ],
    }
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
