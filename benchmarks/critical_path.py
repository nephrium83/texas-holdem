"""How long does a player wait for a hand to start, when seats run in parallel?

Both in-process benchmarks run every seat one after another on one CPU, so
their totals are the SUM of all seats' work. Real seats are separate
machines working at the same time; what a player waits through is the
critical path. This replays a real hand through the MentalDeal coordinators,
measures the CPU cost of every handle() call on every seat, and schedules
those measured costs on per-seat clocks:

  - a seat handles one message at a time, in the order it received them;
  - it starts a message at max(its own clock, the message's arrival);
  - a message it emits leaves when that handle() call finishes;
  - another seat receives it `latency` ms later, the sender at once.

Hand-start delay = when the LAST seat has recovered its hole cards.

Assumptions, stated rather than hidden: costs are measured on this machine
for every seat (identical hardware); in-process FIFO order stands in for
arrival order; latency is per message end to end, so for the host-relayed
star it should be the joiner->host->joiner time, not one hop.

    python critical_path.py --checkout <repo> [--seats 2 4 9] [--hands 3]
"""
import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path


class _Split:
    """Per-call prove and verify seconds, so a what-if can remove either."""

    def __init__(self, bg_shuffle):
        self.mod = bg_shuffle
        self.prove = self.verify = 0.0
        self.orig = (bg_shuffle.prove, bg_shuffle.verify)

    def __enter__(self):
        def wrap(fn, attr):
            def w(*a, **k):
                t0 = time.perf_counter()
                try:
                    return fn(*a, **k)
                finally:
                    setattr(self, attr, getattr(self, attr) + time.perf_counter() - t0)
            return w
        self.mod.prove = wrap(self.orig[0], "prove")
        self.mod.verify = wrap(self.orig[1], "verify")
        return self

    def __exit__(self, *exc):
        self.mod.prove, self.mod.verify = self.orig
        return False

    def take(self):
        out = (self.prove, self.verify)
        self.prove = self.verify = 0.0
        return out


def schedule(trace, seats, hole_at_step, latency, drop=None):
    """Critical path for one latency; `drop` zeroes 'prove' or 'verify'."""
    clock = {s: 0.0 for s in seats}
    sent_at = {}
    finish = []
    for seat, cost, emitted, src, prove, verify in trace:
        if drop == "prove":
            cost -= prove
        elif drop == "verify":
            cost -= verify
        arrival = 0.0 if src is None else (
            sent_at[src[0]] + (0.0 if seat == src[1] else latency))
        start = max(clock[seat], arrival)
        clock[seat] = start + cost
        for e in emitted:
            sent_at[e] = clock[seat]
        finish.append(clock[seat])
    return max(finish[hole_at_step[s]] for s in seats)


def one_hand(seats, prevention, hand_no, latencies, MentalDeal, Phase, split):
    deals = {s: MentalDeal(session_id="cp", hand_no=hand_no, seat=s,
                           seats_in=list(seats), button=0,
                           master_secret=f"seat-{s}-secret".encode(),
                           prevention=prevention) for s in seats}
    # One replay, recording every (seat, message index) cost, then schedule
    # the same costs under each latency. Replaying once keeps every latency
    # on identical work.
    trace = []          # (seat, cost_s, emitted_message_ids, input_msg_id)
    queue = []          # (msg_id, msg, sender)
    next_id = 0
    for s in seats:
        split.take()
        t0 = time.perf_counter()
        out = deals[s].start()
        cost = time.perf_counter() - t0
        ids = []
        for m in out:
            queue.append((next_id, m, s))
            ids.append(next_id)
            next_id += 1
        trace.append((s, cost, ids, None, *split.take()))
    hole_at_step = {}
    while queue:
        mid, msg, sender = queue.pop(0)
        for r in seats:
            split.take()
            t0 = time.perf_counter()
            out = deals[r].handle(dict(msg))
            cost = time.perf_counter() - t0
            ids = []
            for m in out:
                queue.append((next_id, m, r))
                ids.append(next_id)
                next_id += 1
            trace.append((r, cost, ids, (mid, sender), *split.take()))
            if r not in hole_at_step and deals[r].hole_complete():
                hole_at_step[r] = len(trace) - 1
    for s in seats:
        if deals[s].phase != Phase.DEAL or not deals[s].hole_complete():
            raise RuntimeError(f"seat {s} did not reach its hole cards")

    cpu_sum = sum(e[1] for e in trace)
    prove_sum = sum(e[4] for e in trace)
    verify_sum = sum(e[5] for e in trace)
    results = {lat: {drop: schedule(trace, seats, hole_at_step, lat / 1000.0, drop)
                     for drop in (None, "verify", "prove")}
               for lat in latencies}
    return cpu_sum, prove_sum, verify_sum, results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--seats", type=int, nargs="+", default=[2, 4, 9])
    ap.add_argument("--hands", type=int, default=20)
    ap.add_argument("--latency", type=float, nargs="+", default=[0, 25, 50, 100])
    ap.add_argument("--output", type=Path)
    a = ap.parse_args()
    if a.hands < 1 or any(n < 2 or n > 9 for n in a.seats) or any(x < 0 or not math.isfinite(x) for x in a.latency):
        ap.error("use positive hand counts, 2 to 9 seats, and finite nonnegative latency")
    sys.path.insert(0, str(a.checkout))
    from holdem import native_crypto
    native_crypto.initialize()
    from holdem.p2p.mental_deal import MentalDeal, Phase

    from holdem.p2p import bg_shuffle
    if __package__:
        from .benchmark_support import metadata, peak_rss_bytes, summary
    else:
        from benchmark_support import metadata, peak_rss_bytes, summary
    report = {"metadata": metadata(a.checkout), "configurations": [],
              "scope": "modeled parallel wait from measured coordinator wall costs; excludes real transport and bandwidth contention"}
    print("prevention (Bayer-Groth); p50 over %d warm hands; ms" % a.hands)
    print("%-6s %10s %8s %8s | %-9s %8s %14s %14s" % (
        "seats", "CPU sum", "prove", "verify", "latency", "wait",
        "if verify=0", "if prove=0"))
    with _Split(bg_shuffle) as split:
        for n in a.seats:
            seats = list(range(n))
            first = one_hand(seats, True, 1, a.latency, MentalDeal, Phase, split)
            runs = [one_hand(seats, True, h, a.latency, MentalDeal, Phase, split)
                    for h in range(2, a.hands + 2)]
            report["configurations"].append({
                "seats": n, "hands": a.hands, "first_hand_total_ms": first[0] * 1000,
                "peak_rss_bytes": peak_rss_bytes(),
                "aggregate_work_ms": summary([r[0] * 1000 for r in runs]),
                "latencies": [{"latency_ms": lat,
                    "wait_ms": summary([r[3][lat][None] * 1000 for r in runs]),
                    "without_proving_ms": summary([r[3][lat]["prove"] * 1000 for r in runs]),
                    "without_verifying_ms": summary([r[3][lat]["verify"] * 1000 for r in runs])}
                    for lat in a.latency]})
            med = statistics.median
            cpu = med(r[0] for r in runs) * 1000
            pr = med(r[1] for r in runs) * 1000
            ve = med(r[2] for r in runs) * 1000
            for lat in a.latency:
                waits = sorted(r[3][lat][None] for r in runs)
                # Nearest rank, as benchmarks/mental_deal_startup.py uses.
                p95 = waits[math.ceil(len(waits) * 0.95) - 1]
                print("%-6d p95 wait @%gms: %.0f ms (max %.0f) over %d hands"
                      % (n, lat, p95 * 1000, waits[-1] * 1000, len(waits)))
            for i, lat in enumerate(a.latency):
                w = med(r[3][lat][None] for r in runs) * 1000
                wv = med(r[3][lat]["verify"] for r in runs) * 1000
                wp = med(r[3][lat]["prove"] for r in runs) * 1000
                lead = ("%-6d %10.0f %8.0f %8.0f" % (n, cpu, pr, ve)) if i == 0 else " " * 35
                print("%s | %-9s %8.0f %8.0f (%3.0f%%) %8.0f (%3.0f%%)" % (
                    lead, "%gms" % lat, w, wv, 100 * (w - wv) / w, wp, 100 * (w - wp) / w),
                    flush=True)

    if a.output:
        a.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
