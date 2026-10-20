"""Run the documented famous-hand replays and save their action traces."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from famous_hands.replay import AnteRuleMismatch, replay_case


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build/famous-hands-results.json")
    args = parser.parse_args(argv)
    cases = json.loads((ROOT / "tests/famous_hands/cases.json").read_text(encoding="utf-8"))
    reports = []
    for case in cases:
        try:
            report = replay_case(case)
        except (AssertionError, ValueError, RuntimeError) as error:
            report = dict(id=case["id"], title=case["title"], passed=False,
                          names=case["names"], initial_stacks=case["stacks"], holes=case["holes"],
                          status="RULE_MISMATCH" if isinstance(error, AnteRuleMismatch) else "FAIL",
                          error=str(error), coverage=case["coverage"],
                          notes=case["notes"], sources=case["sources"])
            if isinstance(error, AnteRuleMismatch):
                report.update(error.evidence)
        reports.append(report)
        print(f"{'PASS' if report['passed'] else report['status']} {case['title']}"
              + (f": {report['error']}" if not report["passed"] else ""))
    output = dict(cases=reports, passed=sum(case["passed"] for case in reports),
                  failed=sum(not case["passed"] for case in reports),
                  scope="Production betting replicas and settlement; supplied cards, scripted human actions. No bot, Godot, transport or cryptographic-dealing acceptance.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"Report: {args.output}")
    return int(output["failed"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())
