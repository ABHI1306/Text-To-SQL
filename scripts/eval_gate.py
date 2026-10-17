"""D9-2 accuracy gate: read an execution-accuracy report and exit non-zero when it falls short.

    uv run -- python scripts/eval_gate.py reports/eval-20261020.json [--target 0.70] [--p50 20]

Passes only when the report is a real baseline (approved references, not provisional drafts),
execution accuracy is at or above the target set on Day 3 (default 70%), and the median question
latency is within the target (default 20 s). Prints one line per check.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_TARGET = 0.70
DEFAULT_P50_SECONDS = 20.0


def gate(report: dict[str, Any], target: float, p50_limit: float) -> list[tuple[bool, str]]:
    status = report.get("status")
    accuracy = report.get("execution_accuracy")
    p50 = (report.get("latency_seconds") or {}).get("p50")
    return [
        (status == "baseline", f"references are approved (status={status!r}, need 'baseline')"),
        (
            isinstance(accuracy, (int, float)) and accuracy >= target,
            f"execution accuracy {accuracy!r} >= {target:.2f}",
        ),
        (
            isinstance(p50, (int, float)) and p50 <= p50_limit,
            f"median latency {p50!r} s <= {p50_limit:g} s",
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("report", type=Path)
    parser.add_argument("--target", type=float, default=DEFAULT_TARGET)
    parser.add_argument("--p50", type=float, default=DEFAULT_P50_SECONDS)
    args = parser.parse_args()
    results = gate(json.loads(args.report.read_text()), args.target, args.p50)
    for ok, text in results:
        print(f"{'PASS' if ok else 'FAIL'}  {text}")
    return 0 if all(ok for ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
