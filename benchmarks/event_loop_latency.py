"""Measure event-loop responsiveness while cryptographic work runs.

The transport delivers each frame by calling the registered on_message
callbacks inline, on the event-loop thread. The session's handler verifies
Bayer-Groth shuffle proofs, which take tens of milliseconds each, so every
verification is time the loop cannot read a socket, service a timeout, or
answer another peer.

This measures how late a trivial callback runs while that work is in
flight. The maximum is the number that matters: a mean hides exactly the
stall being looked for.
"""
from __future__ import annotations

import argparse
import json
import math
import platform
import statistics
import threading
import time

from holdem.p2p import bg_shuffle, elgamal as eg, ristretto as R, transport as T
from holdem.p2p.mental_deal import BG_M, BG_N, bg_commitment_key
from holdem.p2p.shuffle_mp import random_permutation


def _statement():
    pk = R.mul_base(R.random_scalar())
    in_deck = eg.make_trivial_deck()
    perm = random_permutation(len(in_deck))
    scalars = [R.random_scalar() for _ in range(len(in_deck))]
    out_deck = [eg.reencrypt(pk, in_deck[src], scalars[i])
                for i, src in enumerate(perm)]
    ctx = b"loop-latency"
    proof = bg_shuffle.prove(pk, bg_commitment_key(), in_deck, out_deck,
                             perm, scalars, BG_M, BG_N, ctx)
    return pk, in_deck, out_deck, ctx, proof


def _sample_latency(loop, stop_event, out: list) -> None:
    """Record how late a no-op callback runs, repeatedly, until stopped."""
    while not stop_event.is_set():
        sent = time.perf_counter()
        done = threading.Event()

        def _mark():
            out.append((time.perf_counter() - sent) * 1000)
            done.set()

        try:
            loop.call_soon_threadsafe(_mark)
        except RuntimeError:
            return
        while not done.wait(timeout=0.05):
            if stop_event.is_set():
                return
        time.sleep(0.002)


def _stats(values) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50": round(statistics.median(ordered), 3),
        "p95": round(ordered[math.ceil(len(ordered) * .95) - 1], 3),
        "max": round(max(ordered), 3),
    }


def measure(verifications: int, *, completion_timeout: float = 120) -> dict:
    if isinstance(verifications, bool) or not isinstance(verifications, int) or verifications < 1:
        raise ValueError("verifications must be a positive integer")
    pk, in_deck, out_deck, ctx, proof = _statement()
    T.stop()
    T.start_host(0)
    loop = T.event_loop()

    latencies: list = []
    stop_event = threading.Event()
    sampler = threading.Thread(target=_sample_latency,
                               args=(loop, stop_event, latencies),
                               daemon=True)
    # Run the verifications ON the loop, exactly as an inline on_message
    # callback would.
    done = threading.Event()
    verify_ms: list = []
    failures: list[Exception] = []

    def _work():
        try:
            for _ in range(verifications):
                started = time.perf_counter()
                if not bg_shuffle.verify(pk, bg_commitment_key(), in_deck, out_deck,
                                         BG_M, BG_N, ctx, proof):
                    raise RuntimeError("benchmark proof failed verification")
                verify_ms.append((time.perf_counter() - started) * 1000)
        except Exception as exc:
            failures.append(exc)
        finally:
            done.set()

    sampler_started = False
    try:
        sampler.start()
        sampler_started = True
        time.sleep(0.2)                  # baseline while the loop is idle
        baseline = list(latencies)
        loop.call_soon_threadsafe(_work)
        if not done.wait(timeout=completion_timeout):
            raise TimeoutError("verification workload did not finish")
        if failures:
            raise RuntimeError("verification workload failed") from failures[0]
        if len(verify_ms) != verifications:
            raise RuntimeError("verification workload was incomplete")
        time.sleep(0.05)
        under_load = latencies[len(baseline):]
    finally:
        stop_event.set()
        if sampler_started:
            sampler.join(timeout=5)
        T.stop()
    if sampler.is_alive():
        raise RuntimeError("latency sampler did not stop")

    return {
        "verifications": verifications,
        "verify_ms": _stats(verify_ms),
        "loop_latency_idle_ms": _stats(baseline),
        "loop_latency_under_load_ms": _stats(under_load),
    }


def main() -> None:
    from holdem import native_crypto
    native_crypto.initialize()
    parser = argparse.ArgumentParser()
    parser.add_argument("--verifications", type=int, nargs="+",
                        default=[1, 9, 81],
                        help="81 = a nine-seat hand's full verification load")
    parser.add_argument("--output")
    args = parser.parse_args()

    report = {
        "metadata": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "libsodium": R.libsodium_version(),
            "note": "verifications run ON the event loop, as inline "
                    "on_message delivery does today",
        },
        "runs": [measure(n) for n in args.verifications],
    }
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(encoded)
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
