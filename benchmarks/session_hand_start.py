"""Hand-start cost through real Sessions over signed envelopes (issue #16).

benchmarks/mental_deal_startup.py drives the MentalDeal coordinators
directly and says so: it excludes the Session layer and transport. This
measures what that leaves out, on the production arrangement -- real
identities, the real admission handshake, seats bound to signing keys,
every message signed with wire.pack and verified with wire.unpack (the
SignedBus from tests/test_session_prevention.py) -- and attributes the time:

  prove / verify       Bayer-Groth shuffle proofs (same probes as the
                       coordinator benchmark)
  encode / decode      bg_wire proof serialization
  sign                 wire.pack, one per message sent
  envelope_verify      wire.unpack; the bus verifies ONCE per message,
                       production verifies once per RECEIVER, so the
                       per-call cost is reported to scale from
  session              everything else: Session ingress, authorization,
                       sequencing, relay, the coordinator's own work

A sample is the first hand of a freshly admitted table in a warm process.
Table setup (identities, admission, roster) is excluded from the timing.
Transport is still excluded: delivery is in-process.

    python session_hand_start.py --checkout <repo> [--seats 2 4 9] [--samples 4]
"""
import argparse
import json
import math
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path


class Probe:
    def __init__(self):
        self.calls = 0
        self.seconds = 0.0
        self.bytes = 0

    def wrap(self, fn):
        def wrapped(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
                if isinstance(result, bytes):
                    self.bytes += len(result)
                return result
            finally:
                self.seconds += time.perf_counter() - t0
                self.calls += 1
        return wrapped


def _sample(n, signed_table, modules, tmp):
    bg_shuffle, bg_wire, wire = modules
    bus, sessions, order, _keys = signed_table(tmp, n)
    names = [f"P{i}" for i in range(n)]

    probes = {k: Probe() for k in
              ("prove", "verify", "encode", "decode", "sign", "envelope_verify")}
    originals = (bg_shuffle.prove, bg_shuffle.verify, bg_wire.encode,
                 bg_wire.decode, wire.pack, wire.unpack)
    bg_shuffle.prove = probes["prove"].wrap(originals[0])
    bg_shuffle.verify = probes["verify"].wrap(originals[1])
    bg_wire.encode = probes["encode"].wrap(originals[2])
    bg_wire.decode = probes["decode"].wrap(originals[3])
    wire.pack = probes["sign"].wrap(originals[4])
    wire.unpack = probes["envelope_verify"].wrap(originals[5])
    log_before = len(bus.log)
    cpu_started = time.process_time()
    try:
        t0 = time.perf_counter()
        for cid in order:
            sessions[cid].start_p2p_hand(hand_no=1, names=names,
                                         stacks=[1000] * n, sb=5, bb=10,
                                         button=0)
        bus.drain()
        total = time.perf_counter() - t0
    finally:
        (bg_shuffle.prove, bg_shuffle.verify, bg_wire.encode, bg_wire.decode,
         wire.pack, wire.unpack) = originals

    for cid in order:
        s = sessions[cid]
        if not s._own_hole_set or s.terminal_state is not None or s.hand_voided:
            raise RuntimeError(f"{cid} did not reach its hole cards: "
                               f"{s.terminal_state} {s.void_reason}")
    messages = len(bus.log) - log_before
    ms = {k: p.seconds * 1000 for k, p in probes.items()}
    accounted = sum(ms.values())
    return {
        "seats": n,
        "cpu_ms": (time.process_time() - cpu_started) * 1000,
        "total_ms": total * 1000,
        "prove_ms": ms["prove"], "prove_calls": probes["prove"].calls,
        "verify_ms": ms["verify"], "verify_calls": probes["verify"].calls,
        "serialize_ms": ms["encode"] + ms["decode"],
        "sign_ms": ms["sign"], "sign_calls": probes["sign"].calls,
        "envelope_verify_ms": ms["envelope_verify"],
        "envelope_verify_calls": probes["envelope_verify"].calls,
        "session_ms": total * 1000 - accounted,
        "messages": messages,
        "signed_envelope_bytes": probes["sign"].bytes,
    }


def sample(n, signed_table, modules):
    with tempfile.TemporaryDirectory(prefix="bench16-") as tmp:
        return _sample(n, signed_table, modules, Path(tmp))


def p(values, q):
    values = sorted(values)
    return values[math.ceil(len(values) * q) - 1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--seats", type=int, nargs="+", default=[2, 4, 9])
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--output", type=Path)
    a = ap.parse_args()
    if a.samples < 1 or any(n < 2 or n > 9 for n in a.seats):
        ap.error("use at least one sample and 2 to 9 seats")
    sys.path.insert(0, str(a.checkout))
    sys.path.insert(0, str(a.checkout / "tests"))
    from crypto_gate import initialize_for_tests
    initialize_for_tests()
    from holdem.p2p import bg_shuffle, bg_wire, wire
    from holdem.p2p import ristretto as R
    from tests.test_session_prevention import signed_table

    if __package__:
        from .benchmark_support import metadata, peak_rss_bytes
    else:
        from benchmark_support import metadata, peak_rss_bytes
    report = {"metadata": {**metadata(a.checkout),
        "python": platform.python_version(), "platform": platform.platform(),
        "libsodium": R.libsodium_version(), "measures":
        "real Sessions over signed envelopes, in-process delivery; first hand "
        "of a freshly admitted table in a warm process"}, "configurations": []}
    for n in a.seats:
        first = sample(n, signed_table, (bg_shuffle, bg_wire, wire))
        rows = [sample(n, signed_table, (bg_shuffle, bg_wire, wire))
                for _ in range(a.samples)]
        keys = [k for k in rows[0] if k.endswith("_ms")]
        summary = {k: {"p50": round(statistics.median(r[k] for r in rows), 1),
                       "p95": round(p([r[k] for r in rows], .95), 1),
                       "max": round(max(r[k] for r in rows), 1)} for k in keys}
        counts = {k: rows[0][k] for k in rows[0] if k.endswith("_calls") or k == "messages"}
        report["configurations"].append({"seats": n, "samples": a.samples,
                                         "summary": summary, "counts": counts,
                                         "first_hand": first, "warm_records": rows,
                                         "peak_rss_bytes": peak_rss_bytes()})
        s = summary
        print("%d seats: total p50 %.0f ms | prove %.0f | verify %.0f | serialize %.0f"
              " | sign %.0f (%d) | envelope verify %.0f (%d) | session %.0f | %d messages"
              % (n, s["total_ms"]["p50"], s["prove_ms"]["p50"], s["verify_ms"]["p50"],
                 s["serialize_ms"]["p50"], s["sign_ms"]["p50"], counts["sign_calls"],
                 s["envelope_verify_ms"]["p50"], counts["envelope_verify_calls"],
                 s["session_ms"]["p50"], counts["messages"]), flush=True)
    if a.output:
        a.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
