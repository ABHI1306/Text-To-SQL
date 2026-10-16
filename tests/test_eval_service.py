"""Evaluation runs: scoring, stage triage, idempotency and the diff against the previous run."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.adapters.metadata.collab import InMemoryCollabStore
from app.agent.service import AskResult
from app.agent.state import StructuredFailure
from app.evaluation.execution import EvalService, normalized_rows, render_markdown
from tests.support.audit import RecordingAudit

CASES = [
    {"id": "c1", "question": "q1", "expected_models": ["orders"], "reference_logical_sql": "r1"},
    {"id": "c2", "question": "q2", "expected_models": ["orders"], "reference_logical_sql": "r2"},
    {"id": "c3", "question": "q3", "expected_models": ["orders"], "reference_logical_sql": "r3"},
    {"id": "c4", "question": "q4", "expected_models": ["orders"], "reference_logical_sql": "bad"},
]


def answer(ok: bool = True, rows: Any = ((1,),), models: Any = ("orders",), code: str = "") -> AskResult:
    return AskResult(
        ok=ok, logical_sql="SELECT 1" if ok else None, sql=None, columns=["a"],
        rows=[list(r) for r in rows] if ok else [], truncated=False,
        retrieved_models=list(models), policy_context=None, attempts=0, elapsed_ms=1,
        failure=None if ok else StructuredFailure(code, "nope"),
    )


class Backend:
    def __init__(self, replies: dict[str, AskResult]) -> None:
        self.replies = replies

    def ask(self, question: str) -> AskResult:
        return self.replies[question]

    def reference_rows(self, logical_sql: str) -> Any:
        return "UNKNOWN_COLUMN: bad reference" if logical_sql == "bad" else [[1]]

    def close(self) -> None:
        return None


def service(store: InMemoryCollabStore, replies: dict[str, AskResult]) -> EvalService:
    return EvalService(
        store, lambda _p, _u: Backend(replies), RecordingAudit(),
        cases=lambda: (CASES, 2), spawn=lambda work: work(),
    )


def test_rows_compare_order_insensitively_and_numbers_by_value() -> None:
    assert normalized_rows([[2, "b"], [1, "a"]]) == normalized_rows([[1, "a"], [2, "b"]])
    assert normalized_rows([[1.0]]) == normalized_rows([[Decimal(1)]])


def test_a_run_scores_each_case_and_triages_failures_by_stage() -> None:
    store = InMemoryCollabStore()
    replies = {
        "q1": answer(),
        "q2": answer(rows=((9,),)),
        "q3": answer(ok=False, code="UNKNOWN_COLUMN"),
        "q4": answer(),
    }
    run, created = service(store, replies).start("p", "u", "key")
    assert created
    stored = store.get_eval_run(run["id"])
    assert stored["status"] == "completed"
    by_case = {r["case_id"]: r for r in store.list_eval_results(run["id"])}
    assert by_case["c1"]["outcome"] == "correct"
    assert (by_case["c2"]["outcome"], by_case["c2"]["stage"]) == ("wrong_rows", "generation")
    assert (by_case["c3"]["outcome"], by_case["c3"]["stage"]) == ("failed", "compile")
    assert by_case["c4"]["outcome"] == "reference_invalid"
    summary = stored["summary"]
    assert summary["scored"] == 3 and summary["correct"] == 1
    assert summary["failures_by_stage"] == {"compile": 1, "generation": 1}
    assert summary["execution_accuracy"] == 0.333


def test_a_retrieval_miss_is_blamed_on_retrieval() -> None:
    store = InMemoryCollabStore()
    replies = {q: answer(rows=((9,),), models=()) for q in ("q1", "q2", "q3", "q4")}
    run, _ = service(store, replies).start("p", "u", "key")
    stages = {r["stage"] for r in store.list_eval_results(run["id"]) if r["outcome"] == "wrong_rows"}
    assert stages == {"retrieval"}


def test_the_same_idempotency_key_starts_one_run() -> None:
    store = InMemoryCollabStore()
    svc = service(store, {q: answer() for q in ("q1", "q2", "q3", "q4")})
    first, created_first = svc.start("p", "u", "same")
    second, created_second = svc.start("p", "u", "same")
    assert first["id"] == second["id"] and created_first and not created_second
    assert len(store.eval_runs) == 1


def test_the_report_diffs_against_the_previous_run_and_renders_markdown() -> None:
    store = InMemoryCollabStore()
    good = {q: answer() for q in ("q1", "q2", "q3", "q4")}
    bad = {**good, "q1": answer(ok=False, code="TIMEOUT")}
    first, _ = service(store, good).start("p", "u", "k1")
    second, _ = service(store, bad).start("p", "u", "k2")
    report = service(store, bad).report(second["id"])
    assert report is not None
    assert [r["case_id"] for r in report["diff"]["regressions"]] == ["c1"]
    assert report["diff"]["previous_run_id"] == first["id"]
    text = render_markdown(report)
    assert "regression: c1" in text and "| c1 | failed | execution | TIMEOUT |" in text


def test_a_backend_that_cannot_open_marks_the_run_failed() -> None:
    store = InMemoryCollabStore()

    def broken(_p: str, _u: str) -> Backend:
        raise LookupError("no access")

    svc = EvalService(store, broken, RecordingAudit(), cases=lambda: (CASES, 0), spawn=lambda w: w())
    run, _ = svc.start("p", "u", "k")
    assert store.get_eval_run(run["id"])["status"] == "failed"
