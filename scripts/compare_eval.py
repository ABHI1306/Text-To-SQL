"""Cluster one evaluation run's failures by stage and diff it against an earlier run.

    uv run -- python scripts/compare_eval.py reports/new.json [--baseline reports/old.json]
        [--lever "Metadata: described order_items.amount"] [--log reports/lever-log.md]

Change one lever per run so each delta has one cause. With --lever and --log, one line (date,
lever, accuracy, delta, cases fixed and broken) is appended to the log. Golden references are read
only here and never edited.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from app.evaluation.execution import _STAGE_BY_CODE

LEVERS = (
    "metadata", "sql pairs", "instructions", "retrieval", "prompts and validator messages",
    "compiler coverage", "sampling or schema linking",
)


def stage_of(case: dict[str, Any]) -> str | None:
    """Where a case went wrong, or None when it was correct."""
    outcome = case["outcome"]
    if outcome == "correct":
        return None
    if outcome == "reference_invalid":
        return "bad reference"
    if outcome == "wrong_rows":
        return "generation error"
    return _STAGE_BY_CODE.get(str(case.get("failure")), f"other ({case.get('failure')})")


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def clusters(report: dict[str, Any]) -> Counter[str]:
    return Counter(stage for case in report["cases"] if (stage := stage_of(case)))


def diff(new: dict[str, Any], old: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Case ids that became correct, and ids that stopped being correct."""
    before = {c["id"]: c["outcome"] == "correct" for c in old["cases"]}
    fixed, broken = [], []
    for case in new["cases"]:
        was, now = before.get(case["id"]), case["outcome"] == "correct"
        if was is False and now:
            fixed.append(case["id"])
        elif was and not now:
            broken.append(case["id"])
    return fixed, broken


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--lever", default="")
    parser.add_argument("--log", type=Path)
    args = parser.parse_args()

    new = load(args.run)
    print(f"accuracy {new['execution_accuracy']}  scored {new['scored']}")
    print("failures by stage:")
    for stage, count in clusters(new).most_common():
        print(f"  {count:3} {stage}")
    fixed: list[str] = []
    broken: list[str] = []
    delta = ""
    if args.baseline:
        old = load(args.baseline)
        fixed, broken = diff(new, old)
        gain = (new["execution_accuracy"] or 0) - (old["execution_accuracy"] or 0)
        delta = f"{gain:+.3f}"
        print(f"vs baseline {old['execution_accuracy']}: {delta}; fixed {fixed}; broken {broken}")
    if args.lever and args.log:
        if not any(args.lever.lower().startswith(name) for name in LEVERS):
            parser.error(f"--lever should start with one of: {', '.join(LEVERS)}")
        line = (f"| {date.today().isoformat()} | {args.lever} | {new['execution_accuracy']} "
                f"| {delta or 'n/a'} | {', '.join(fixed) or '-'} | {', '.join(broken) or '-'} |\n")
        if not args.log.exists():
            args.log.write_text("| date | lever | accuracy | delta | fixed | broken |\n"
                                "|---|---|---|---|---|---|\n")
        with args.log.open("a") as handle:
            handle.write(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
