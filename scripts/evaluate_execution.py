"""Execution-accuracy runner v1: model answers vs reference queries, through the full pipeline.

    docker compose exec api uv run --no-sync python scripts/evaluate_execution.py \\
        --project-id <uuid> --username <account> [--include-draft] [--case-prefix ecom-]

Both the model's query and the reference run through the compiler under the same account, so row
policies apply equally. Rows are compared order-insensitively. Reference queries that do not
compile are reported separately and not scored. Without approved references nothing is measured
unless --include-draft is given, and that result is labelled provisional.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.cli.ask import CliContext, ask, open_context
from app.core.types import ValidatedQuery
from app.semantic.compiler import compile_query

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/data/retrieval_golden.json"
REFERENCES = ROOT / "tests/data/execution_references.json"


def _cell(value: Any) -> str:
    if isinstance(value, (Decimal, float)):
        return f"{float(value):.2f}"
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:19]
    return str(value)


def _rows(rows: Any) -> list[tuple[str, ...]]:
    return sorted(tuple(_cell(cell) for cell in row) for row in rows)


def _reference_rows(context: CliContext, logical_sql: str) -> list[tuple[str, ...]] | str:
    compiled = compile_query(logical_sql, context.mdl, context.principal, context.policies)
    if not compiled.ok:
        return "; ".join(f"{e.code.value}: {e.message}" for e in compiled.errors)
    # Executed through the same adapter the service uses, i.e. read-only with a timeout.
    warehouse = context.service._warehouse
    try:
        result = warehouse.execute_read_only(
            ValidatedQuery(compiled.sql, compiled.parameters), max_rows=1000
        )
    except Exception as error:  # noqa: BLE001 - reported per case
        return f"execution failed ({getattr(error, 'sqlstate', type(error).__name__)})"
    return _rows(result.rows)


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))] if ordered else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--case-prefix", default="")
    parser.add_argument("--include-draft", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    golden = {c["id"]: c for c in json.loads(GOLDEN.read_text())["cases"]}
    references = json.loads(REFERENCES.read_text())["references"]
    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    unmeasured = 0
    for case_id, case in golden.items():
        if not case_id.startswith(args.case_prefix):
            continue
        reference = references.get(case_id)
        usable = reference and (reference["approval_status"] == "approved" or args.include_draft)
        if usable:
            selected.append((case, reference))
        else:
            unmeasured += 1
    if args.limit:
        selected = selected[: args.limit]
    if not selected:
        print(json.dumps({"status": "not_measurable", "reason": "no approved references"}))
        return 0

    context = open_context(args.project_id, args.username, audit=False)
    results: list[dict[str, Any]] = []
    try:
        for case, reference in selected:
            expected = _reference_rows(context, reference["logical_sql"])
            started = time.monotonic()
            answer = ask(context, case["question"], include_compiled=True)
            seconds = time.monotonic() - started
            row: dict[str, Any] = {
                "id": case["id"],
                "question": case["question"],
                "difficulty": case["difficulty"],
                "seconds": round(seconds, 1),
                "attempts": answer.attempts,
                "logical_sql": answer.logical_sql,
                "first_attempt_valid": answer.ok and answer.attempts == 0,
            }
            if isinstance(expected, str):
                row.update(outcome="reference_invalid", detail=expected)
            elif not answer.ok:
                row.update(outcome="failed", failure=answer.failure.code if answer.failure else None)
            else:
                matched = _rows(answer.rows) == expected
                row.update(outcome="correct" if matched else "wrong_rows")
            results.append(row)
            print(f"{row['id']:8} {row['outcome']:18} {row['seconds']:>6}s  attempts={row['attempts']}", file=sys.stderr)
    finally:
        context.close()

    scored = [r for r in results if r["outcome"] != "reference_invalid"]
    correct = [r for r in scored if r["outcome"] == "correct"]
    first_failed = [r for r in scored if not r["first_attempt_valid"]]
    recovered = [r for r in first_failed if r["outcome"] in {"correct", "wrong_rows"}]
    latencies = [r["seconds"] for r in scored]
    report = {
        "status": "provisional_draft_references" if args.include_draft else "baseline",
        "note": "References are unapproved drafts; do not treat as the baseline until a DBA approves them."
        if args.include_draft else "",
        "date": datetime.now().date().isoformat(),
        "account": args.username,
        "sql_model": __import__("app.config", fromlist=["get_settings"]).get_settings().sql_model,
        "scored": len(scored),
        "unmeasured_no_reference": unmeasured,
        "reference_invalid": [r["id"] for r in results if r["outcome"] == "reference_invalid"],
        "execution_accuracy": round(len(correct) / len(scored), 3) if scored else None,
        "validity_rate_first_attempt": round(sum(r["first_attempt_valid"] for r in scored) / len(scored), 3) if scored else None,
        "correction_success": round(len(recovered) / len(first_failed), 3) if first_failed else None,
        "latency_seconds": {"p50": round(statistics.median(latencies), 1) if latencies else None, "p95": _percentile(latencies, 0.95)},
        "outcomes": {o: sum(r["outcome"] == o for r in results) for o in sorted({r["outcome"] for r in results})},
        "cases": results,
    }
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        args.out.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
