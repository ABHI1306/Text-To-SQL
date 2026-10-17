"""The accuracy gate: provisional or short reports fail, a real baseline at target passes."""

from __future__ import annotations

from scripts.eval_gate import gate


def _report(**changes: object) -> dict[str, object]:
    return {"status": "baseline", "execution_accuracy": 0.72, "latency_seconds": {"p50": 14.0}, **changes}


def test_a_baseline_at_target_passes() -> None:
    assert all(ok for ok, _ in gate(_report(), 0.70, 20))


def test_the_exact_target_passes() -> None:
    assert all(ok for ok, _ in gate(_report(execution_accuracy=0.70), 0.70, 20))


def test_provisional_draft_references_never_pass() -> None:
    assert not all(ok for ok, _ in gate(_report(status="provisional_draft_references"), 0.70, 20))


def test_low_accuracy_or_slow_median_fails() -> None:
    assert not all(ok for ok, _ in gate(_report(execution_accuracy=0.2), 0.70, 20))
    assert not all(ok for ok, _ in gate(_report(latency_seconds={"p50": 69.3}), 0.70, 20))


def test_a_report_that_measured_nothing_fails() -> None:
    assert not all(ok for ok, _ in gate({"status": "not_measurable"}, 0.70, 20))
