"""Day 9 tracing and metrics: token and GPU counts per question, process totals, ops routes."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.llm.ollama import OllamaLLMAdapter
from app.api.dependencies import get_current_context
from app.api.ops_routes import router as ops_router
from app.auth.models import AuthContext
from app.observability.usage import METRICS, Usage, bind_usage, counts_from_ollama, record_call


@pytest.fixture(autouse=True)
def fresh_metrics() -> None:
    METRICS.reset()


def test_calls_inside_a_bound_scope_add_up_and_feed_the_process_totals() -> None:
    usage = Usage()
    with bind_usage(usage):
        record_call(100, 20, 2_000_000_000)
        record_call(50, 10, 500_000_000)
    record_call(1, 1, 0)  # outside any question: totals only
    assert (usage.prompt_tokens, usage.completion_tokens, usage.gpu_ms, usage.calls) == (150, 30, 2500, 2)
    assert METRICS.snapshot()["llm_calls"] == 3


def test_negative_counts_are_ignored() -> None:
    usage = Usage()
    with bind_usage(usage):
        record_call(-5, -5, -5)
    assert usage.tokens == 0 and usage.gpu_ms == 0


def test_ollama_counts_tolerate_missing_and_odd_fields() -> None:
    assert counts_from_ollama({"prompt_eval_count": 7, "eval_count": 3, "total_duration": 9}) == (7, 3, 9)
    assert counts_from_ollama({"eval_count": "x", "total_duration": True}) == (0, 0, 0)


def test_the_ollama_adapter_reports_usage_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, object]:
            return {"response": json.dumps({"a": 1}), "prompt_eval_count": 12, "eval_count": 4,
                    "total_duration": 3_000_000_000}

    monkeypatch.setattr("app.adapters.llm.ollama.httpx.post", lambda *a, **k: Response())
    adapter = OllamaLLMAdapter("http://ollama", "m")
    usage = Usage()
    with bind_usage(usage):
        adapter.generate("hi")
    assert (usage.tokens, usage.gpu_ms) == (16, 3000)

    import httpx

    def refuse(*_: object, **__: object) -> None:
        raise httpx.ConnectError("down")

    monkeypatch.setattr("app.adapters.llm.ollama.httpx.post", refuse)
    with pytest.raises(RuntimeError):
        adapter.generate("hi")
    assert METRICS.snapshot()["llm_errors"] == 1


def test_per_question_averages_use_recent_questions() -> None:
    for tokens in (100, 300):
        usage = Usage()
        usage.add(tokens, 0, 1000)
        METRICS.add_question(usage, 800)
    snapshot = METRICS.snapshot()
    assert snapshot["questions"] == 2 and snapshot["avg_tokens_per_question"] == 200
    assert snapshot["avg_gpu_ms_per_question"] == 1000


def _client(capabilities: frozenset[str]) -> TestClient:
    app = FastAPI()
    app.include_router(ops_router)
    app.dependency_overrides[get_current_context] = lambda: AuthContext(
        "u", "u", "Role", capabilities, frozenset(), False
    )
    return TestClient(app)


def test_usage_endpoint_needs_view_audit() -> None:
    assert _client(frozenset({"ask_question"})).get("/api/v1/ops/usage").status_code == 403
    response = _client(frozenset({"view_audit"})).get("/api/v1/ops/usage")
    assert response.status_code == 200 and response.json()["questions"] == 0


def test_scheduler_endpoint_needs_view_audit() -> None:
    assert _client(frozenset({"ask_question"})).get("/api/v1/ops/scheduler").status_code == 403
