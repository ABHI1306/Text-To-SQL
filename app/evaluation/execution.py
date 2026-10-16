"""Execution-accuracy evaluation as a service: runs the golden set through the full pipeline,
stores each case (with the stage that failed), and reports against the previous run.

The CLI script ``scripts/evaluate_execution.py`` is the same measurement for a terminal; this
module is what ``POST /projects/{id}/eval/run`` starts.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from app.agent.service import AskResult
from app.audit.service import AuditService
from app.ports.collab import CollabStorePort, Record

DATA_DIR = Path(__file__).resolve().parents[2] / "tests" / "data"
GOLDEN = DATA_DIR / "retrieval_golden.json"
REFERENCES = DATA_DIR / "execution_references.json"
SUITE = "execution"

# Which part of the pipeline a typed failure points at, for triage.
_STAGE_BY_CODE = {
    "LLM_UNAVAILABLE": "llm",
    "AMBIGUOUS_QUESTION": "intent",
    "OUT_OF_SCOPE": "intent",
    "UNKNOWN_MODEL": "compile",
    "UNKNOWN_COLUMN": "compile",
    "JOIN_NOT_ALLOWED": "compile",
    "NON_SELECT": "compile",
    "DISALLOWED_FUNCTION": "compile",
    "SYNTAX_ERROR": "compile",
    "POLICY_ERROR": "compile",
    "DRY_RUN_FAILED": "validation",
    "TIMEOUT": "execution",
    "EMPTY_RESULT": "execution",
}


class EvalBackend(Protocol):
    """What a run needs from the running system, for one account and project."""

    def ask(self, question: str) -> AskResult: ...

    def reference_rows(self, logical_sql: str) -> list[list[Any]] | str:
        """Rows of a reference query through the same compiler, or a reason it cannot run."""
        ...

    def close(self) -> None: ...


BackendFactory = Callable[[str, str], EvalBackend]  # (project_id, user_id) -> backend


def _cell(value: Any) -> str:
    if isinstance(value, (Decimal, float)):
        return f"{float(value):.2f}"
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:19]
    return str(value)


def normalized_rows(rows: Sequence[Sequence[Any]]) -> list[tuple[str, ...]]:
    """Order-insensitive comparison form of a result set."""
    return sorted(tuple(_cell(cell) for cell in row) for row in rows)


def stage_of(code: str | None) -> str:
    return _STAGE_BY_CODE.get(code or "", "other")


def load_cases(include_draft: bool = True) -> tuple[list[dict[str, Any]], int]:
    """(cases with an usable reference, count of cases that cannot be measured)."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]
    references = json.loads(REFERENCES.read_text(encoding="utf-8"))["references"]
    selected: list[dict[str, Any]] = []
    unmeasured = 0
    for case in golden:
        reference = references.get(case["id"])
        usable = reference and (reference["approval_status"] == "approved" or include_draft)
        if usable:
            selected.append({**case, "reference_logical_sql": reference["logical_sql"],
                             "reference_status": reference["approval_status"]})
        else:
            unmeasured += 1
    return selected, unmeasured


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))] if ordered else 0.0


class EvalService:
    def __init__(
        self,
        store: CollabStorePort,
        backend_for: BackendFactory,
        audit: AuditService | Any,
        *,
        cases: Callable[[], tuple[list[dict[str, Any]], int]] = load_cases,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._store = store
        self._backend_for = backend_for
        self._audit = audit
        self._cases = cases
        self._spawn = spawn or self._thread

    @staticmethod
    def _thread(work: Callable[[], None]) -> None:
        threading.Thread(target=work, daemon=True, name="eval-run").start()

    def start(self, project_id: str, user_id: str, idempotency_key: str) -> tuple[Record, bool]:
        run, created = self._store.create_eval_run(project_id, user_id, idempotency_key)
        if created:
            self._audit.record(user_id, "eval_run_started", "project", project_id,
                               {"run_id": run["id"]})
            self._spawn(lambda: self._execute(run["id"], project_id, user_id))
        return run, created

    def _execute(self, run_id: str, project_id: str, user_id: str) -> None:
        try:
            backend = self._backend_for(project_id, user_id)
        except Exception as error:  # noqa: BLE001 - recorded on the run
            self._store.finish_eval_run(run_id, "failed", None, f"{type(error).__name__}: {error}"[:300])
            return
        try:
            cases, unmeasured = self._cases()
            results = [self._run_case(run_id, backend, case) for case in cases]
            self._store.finish_eval_run(run_id, "completed", self._summarize(results, unmeasured), None)
        except Exception as error:  # noqa: BLE001 - recorded on the run
            self._store.finish_eval_run(run_id, "failed", None, f"{type(error).__name__}: {error}"[:300])
        finally:
            backend.close()
            self._audit.record(user_id, "eval_run_finished", "project", project_id,
                               {"run_id": run_id})

    def _run_case(self, run_id: str, backend: EvalBackend, case: dict[str, Any]) -> Record:
        expected = backend.reference_rows(case["reference_logical_sql"])
        started = time.monotonic()
        answer = backend.ask(case["question"])
        latency_ms = round((time.monotonic() - started) * 1000)
        result: Record = {
            "case_id": case["id"], "suite": SUITE, "question": case["question"],
            "generated_sql": answer.logical_sql, "latency_ms": latency_ms, "gpu_ms": None,
            "exec_match": None, "failure_code": None, "stage": None,
        }
        retrieval_missed = not set(case.get("expected_models") or []) <= set(answer.retrieved_models)
        if isinstance(expected, str):
            result.update(outcome="reference_invalid", stage="reference", failure_code=None)
        elif not answer.ok:
            code = answer.failure.code if answer.failure else None
            result.update(outcome="failed", failure_code=code, exec_match=False,
                          stage="retrieval" if retrieval_missed else stage_of(code))
        else:
            matched = normalized_rows(answer.rows) == normalized_rows(expected)
            result.update(outcome="correct" if matched else "wrong_rows", exec_match=matched,
                          stage=None if matched else ("retrieval" if retrieval_missed else "generation"))
        self._store.add_eval_result(run_id, result)
        return result

    @staticmethod
    def _summarize(results: list[Record], unmeasured: int) -> Record:
        scored = [r for r in results if r["outcome"] != "reference_invalid"]
        correct = [r for r in scored if r["outcome"] == "correct"]
        latencies = [r["latency_ms"] / 1000 for r in scored]
        stages: dict[str, int] = {}
        for r in scored:
            if r["stage"]:
                stages[r["stage"]] = stages.get(r["stage"], 0) + 1
        return {
            "suite": SUITE, "scored": len(scored), "correct": len(correct),
            "execution_accuracy": round(len(correct) / len(scored), 3) if scored else None,
            "unmeasured_no_reference": unmeasured,
            "reference_invalid": [r["case_id"] for r in results if r["outcome"] == "reference_invalid"],
            "failures_by_stage": dict(sorted(stages.items())),
            "latency_seconds": {
                "p50": round(statistics.median(latencies), 2) if latencies else None,
                "p95": round(_percentile(latencies, 0.95), 2),
            },
        }

    def report(self, run_id: str) -> Record | None:
        run = self._store.get_eval_run(run_id)
        if run is None:
            return None
        results = self._store.list_eval_results(run_id)
        previous = self._store.previous_eval_run(str(run["project_id"]), run_id)
        return {"run": run, "results": results, "diff": self._diff(results, previous)}

    def _diff(self, results: list[Record], previous: Record | None) -> Record:
        if previous is None:
            return {"previous_run_id": None, "regressions": [], "fixes": [], "unchanged": len(results)}
        before = {r["case_id"]: r["outcome"] for r in self._store.list_eval_results(str(previous["id"]))}
        regressions, fixes, unchanged = [], [], 0
        for r in results:
            was = before.get(r["case_id"])
            if was is None or was == r["outcome"]:
                unchanged += 1
            elif was == "correct":
                regressions.append({"case_id": r["case_id"], "was": was, "now": r["outcome"]})
            elif r["outcome"] == "correct":
                fixes.append({"case_id": r["case_id"], "was": was, "now": r["outcome"]})
            else:
                unchanged += 1
        return {"previous_run_id": previous["id"], "previous_summary": previous.get("summary"),
                "regressions": regressions, "fixes": fixes, "unchanged": unchanged}


def render_markdown(report: Record) -> str:
    run, results, diff = report["run"], report["results"], report["diff"]
    summary = run.get("summary") or {}
    lines = [
        f"# Evaluation run {run['id']}",
        "",
        f"Status: **{run['status']}**" + (f" ({run['error']})" if run.get("error") else ""),
        "",
    ]
    if summary:
        lines += [
            f"- Execution accuracy: **{summary['execution_accuracy']}** ({summary['correct']}/{summary['scored']} scored)",
            f"- Not measurable (no reference): {summary['unmeasured_no_reference']}",
            f"- Latency p50 / p95: {summary['latency_seconds']['p50']}s / {summary['latency_seconds']['p95']}s",
            f"- Failures by stage: {summary['failures_by_stage'] or 'none'}",
            "",
        ]
    lines += ["## Against the previous run", ""]
    if diff["previous_run_id"] is None:
        lines.append("No earlier completed run.")
    else:
        lines.append(f"Previous run `{diff['previous_run_id']}`: "
                     f"{len(diff['regressions'])} regressions, {len(diff['fixes'])} fixes, "
                     f"{diff['unchanged']} unchanged.")
        lines += [f"- regression: {r['case_id']} ({r['was']} -> {r['now']})" for r in diff["regressions"]]
        lines += [f"- fixed: {r['case_id']} ({r['was']} -> {r['now']})" for r in diff["fixes"]]
    lines += ["", "## Cases", "", "| Case | Outcome | Stage | Failure | ms |", "|---|---|---|---|---|"]
    lines += [
        f"| {r['case_id']} | {r['outcome']} | {r.get('stage') or ''} | {r.get('failure_code') or ''} | {r.get('latency_ms')} |"
        for r in results
    ]
    return "\n".join(lines) + "\n"
