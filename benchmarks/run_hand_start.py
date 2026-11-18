"""Run isolated seat configurations and produce an issue #16 report bundle."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hands", type=int, default=20)
    parser.add_argument("--seats", type=int, nargs="+", default=[2, 4, 9])
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.hands < 1 or any(n < 2 or n > 9 for n in args.seats):
        parser.error("use positive hands and 2 to 9 seats")
    root = Path(__file__).resolve().parents[1]
    args.output_dir.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, PYTHONPATH=str(root))
    reports = []
    for seats in args.seats:
        destination = args.output_dir / f"coordinator-{seats}.json"
        with destination.with_suffix(".log").open("w", encoding="utf-8") as log:
            subprocess.run([sys.executable, str(root / "benchmarks/mental_deal_startup.py"),
                            "--seats", str(seats), "--hands", str(args.hands + 1),
                            "--output", str(destination)], cwd=root, env=env,
                           stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1800)
    for seats in args.seats:
        for script, count_flag in (("session_hand_start", "--samples"), ("critical_path", "--hands")):
            destination = args.output_dir / f"{script}-{seats}.json"
            command = [sys.executable, str(root / "benchmarks" / f"{script}.py"),
                       "--checkout", str(root), "--seats", str(seats),
                       count_flag, str(args.hands), "--output", str(destination)]
            with destination.with_suffix(".log").open("w", encoding="utf-8") as log:
                subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                               check=True, timeout=1800)
            reports.append((script, json.loads(destination.read_text(encoding="utf-8"))))
    lines = ["# Hand-start baseline — issue #16", "",
             "Fresh process per seat configuration. First hand is separate from the warm samples.",
             "RSS includes native allocations, interpreter, setup and caches; it is not per-seat production memory.",
             "No real transport, bandwidth contention, or cross-home upload is measured here.", "",
             "| Harness | Seats | Warm hands | p95 ms | Peak RSS MiB |", "|---|---:|---:|---:|---:|"]
    for script, report in reports:
        config = report["configurations"][0]
        if script == "session_hand_start":
            p95 = config["summary"]["total_ms"]["p95"]
            count = config["samples"]
            name = "Signed Sessions (aggregate work)"
        else:
            p95 = next(row["wait_ms"]["p95"] for row in config["latencies"] if row["latency_ms"] == 0)
            count = config["hands"]
            name = "Modeled parallel wait, 0 ms latency"
        lines.append(f"| {name} | {config['seats']} | {count} | {p95:.1f} | {config['peak_rss_bytes'] / 1048576:.1f} |")
    lines.extend(["", "```json", json.dumps(reports[0][1]["metadata"], indent=2), "```", "",
                  "See individual JSON files for latency scenarios, maxima, component timings and limitations."])
    (args.output_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
