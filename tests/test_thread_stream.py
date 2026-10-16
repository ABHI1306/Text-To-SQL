"""Thread ask stream (section 8 SSE contract), rerun-sql, preview-as and message feedback."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.metadata.collab import InMemoryCollabStore
from app.adapters.metadata.threads import InMemoryThreadStore
from app.api import dependencies, thread_routes
from app.api.ask_routes import _Prepared
from app.auth.models import AuthContext
from app.ports.threads import Turn
from tests.support.audit import RecordingAudit
from tests.support.ecommerce_mdl import build_mdl, build_policies, build_principal
from tests.test_agent_graph import FakeWarehouse, ScriptedLlm, sql
from tests.test_ask_service import MDL, ask, service

PROJECT = "00000000-0000-0000-0000-0000000000a1"
OWNER = "00000000-0000-0000-0000-00000000000a"
OTHER = "00000000-0000-0000-0000-00000000000b"


def contract(svc: Any, **kwargs: Any) -> list[Any]:
    principal = build_principal("u1", {"region": "West"})
    return list(
        svc.stream("p", 1, MDL, principal, build_policies(), "orders?", contract=True, **kwargs)
    )


def test_contract_stream_has_intent_first_and_the_documented_order() -> None:
    events = contract(service(ScriptedLlm(sql("SELECT order_id FROM orders")), RecordingAudit()))
    assert [e.type for e in events] == [
        "intent", "retrieval", "sql_candidate", "compilation", "validation", "rows", "done",
    ]
    assert events[0].data["intent"] == "data_question"
    assert events[0].data["used_context"] is False
    assert {"message_id", "latency_ms", "tokens", "gpu_ms"} <= set(events[-1].data)


def test_the_default_stream_is_unchanged_by_the_contract_option() -> None:
    result = ask(service(ScriptedLlm(sql("SELECT order_id FROM orders")), RecordingAudit()))
    assert "intent" not in {e.type for e in result.events}


def test_llm_failure_ends_in_one_typed_error_event() -> None:
    events = contract(service(ScriptedLlm(sql("DROP TABLE orders")), RecordingAudit()))
    assert events[-1].type == "error"
    assert events[-1].data["code"] == "NON_SELECT" and events[-1].data["retryable"] is False
    assert [e.type for e in events].count("error") == 1


def test_compiled_sql_appears_only_when_asked_for() -> None:
    svc = service(ScriptedLlm(sql("SELECT order_id FROM orders")), RecordingAudit())
    plain = [e for e in contract(svc) if e.type == "compilation"]
    dba = [e for e in contract(svc, include_compiled=True) if e.type == "compilation"]
    assert "sql" not in plain[0].data and "m_orders" in dba[0].data["sql"]


def test_max_rows_can_only_lower_the_limit() -> None:
    warehouse = FakeWarehouse()
    svc = service(ScriptedLlm(sql("SELECT order_id FROM orders")), RecordingAudit(), warehouse)
    contract(svc, max_rows=10**9)
    assert svc._max_rows == 1000  # the configured ceiling is untouched


def test_a_preview_run_reads_the_owners_history_and_writes_nothing() -> None:
    threads = InMemoryThreadStore()
    threads.append("t", "owner", "p", Turn("first", "first", "SELECT 1", None))
    svc = service(ScriptedLlm(sql("SELECT order_id FROM orders")), RecordingAudit())
    svc._threads = threads  # type: ignore[assignment]
    contract(svc, thread_id="t", history_user_id="owner", remember=False)
    assert len(threads.history("t", "owner", limit=10)) == 1


def test_rerun_runs_the_same_gates_and_audits() -> None:
    audit = RecordingAudit()
    svc = service(ScriptedLlm(sql("SELECT 1")), audit)
    principal = build_principal("u1", {"region": "West"})
    events, failure = svc.rerun("p", 1, MDL, principal, build_policies(), "SELECT order_id FROM orders")
    assert failure is None and events[-1].type == "rows"
    events, failure = svc.rerun("p", 1, MDL, principal, build_policies(), "DROP TABLE orders")
    assert failure is not None and failure.code == "NON_SELECT"
    assert audit.actions() == ["sql_rerun", "sql_rerun"]


# ------------------------------------------------------------------ HTTP


def _context(capabilities: set[str], user_id: str = OWNER) -> AuthContext:
    return AuthContext(
        user_id=user_id, username="u", role_name="user", capabilities=frozenset(capabilities),
        project_ids=frozenset({PROJECT}), must_change_password=False,
    )


def _client(
    context: AuthContext, threads: InMemoryThreadStore, svc: Any, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    app = FastAPI()
    app.include_router(thread_routes.router)
    app.dependency_overrides[dependencies.get_current_context] = lambda: context
    app.dependency_overrides[dependencies.get_thread_store] = lambda: threads
    app.dependency_overrides[dependencies.get_collab_store] = lambda: InMemoryCollabStore()
    app.dependency_overrides[dependencies.get_repository] = lambda: MagicMock()
    governance = MagicMock()
    governance.list_policies.return_value = []
    app.dependency_overrides[dependencies.get_governance_repository] = lambda: governance
    app.dependency_overrides[dependencies.get_audit_service] = lambda: RecordingAudit()
    app.dependency_overrides[dependencies.get_knowledge_service] = lambda: MagicMock()
    monkeypatch.setattr(
        thread_routes, "_prepare", lambda *_a, **_k: _Prepared(svc, build_mdl(), 1, lambda: None)
    )
    return TestClient(app, raise_server_exceptions=False)


def _parse(body: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for block in body.strip().split("\n\n"):
        name, data = block.split("\n", 1)
        out.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return out


def _thread(threads: InMemoryThreadStore, owner: str = OWNER) -> str:
    threads.create("11111111-1111-1111-1111-111111111111", PROJECT, owner)
    return "11111111-1111-1111-1111-111111111111"


def test_ask_streams_events_in_order_and_hides_sql_without_rerun_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = InMemoryThreadStore()
    thread = _thread(threads)
    svc = service(ScriptedLlm(sql("SELECT order_id FROM orders")), RecordingAudit())
    client = _client(_context({"ask_question"}), threads, svc, monkeypatch)
    response = client.post(f"/api/v1/threads/{thread}/ask", json={"question": "orders?"})
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse(response.text)
    assert [name for name, _ in events][-1] == "done" and events[0][0] == "intent"
    assert all("sql" not in data for name, data in events if name == "compilation")


def test_ask_shows_compiled_sql_to_accounts_with_rerun_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = InMemoryThreadStore()
    thread = _thread(threads)
    svc = service(ScriptedLlm(sql("SELECT order_id FROM orders")), RecordingAudit())
    client = _client(_context({"ask_question", "rerun_sql"}), threads, svc, monkeypatch)
    events = _parse(client.post(f"/api/v1/threads/{thread}/ask", json={"question": "orders?"}).text)
    assert any("sql" in data for name, data in events if name == "compilation")


def test_a_node_exception_becomes_a_terminal_error_event(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = InMemoryThreadStore()
    thread = _thread(threads)
    svc = MagicMock()
    svc.stream.side_effect = RuntimeError("boom")
    client = _client(_context({"ask_question"}), threads, svc, monkeypatch)
    events = _parse(client.post(f"/api/v1/threads/{thread}/ask", json={"question": "q"}).text)
    assert events == [("error", {"code": "INTERNAL", "message": "The question failed.", "retryable": True})]


def test_another_users_thread_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = InMemoryThreadStore()
    thread = _thread(threads, owner=OTHER)
    client = _client(_context({"ask_question"}), threads, MagicMock(), monkeypatch)
    assert client.post(f"/api/v1/threads/{thread}/ask", json={"question": "q"}).status_code == 404
    assert client.get(f"/api/v1/threads/{thread}").status_code == 404


def test_view_all_threads_can_read_but_not_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = InMemoryThreadStore()
    thread = _thread(threads, owner=OTHER)
    client = _client(_context({"ask_question", "view_all_threads"}), threads, MagicMock(), monkeypatch)
    assert client.get(f"/api/v1/threads/{thread}").status_code == 200
    assert client.post(f"/api/v1/threads/{thread}/ask", json={"question": "q"}).status_code == 404


def test_preview_as_needs_rerun_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = InMemoryThreadStore()
    thread = _thread(threads)
    client = _client(_context({"ask_question"}), threads, MagicMock(), monkeypatch)
    response = client.post(
        f"/api/v1/threads/{thread}/ask", json={"question": "q", "preview_as": OTHER}
    )
    assert response.status_code == 403


def test_a_huge_max_rows_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = InMemoryThreadStore()
    thread = _thread(threads)
    client = _client(_context({"ask_question"}), threads, MagicMock(), monkeypatch)
    response = client.post(
        f"/api/v1/threads/{thread}/ask", json={"question": "q", "max_rows": 10**9}
    )
    assert response.status_code == 422


def test_rerun_sql_is_refused_without_the_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = InMemoryThreadStore()
    thread = _thread(threads)
    client = _client(_context({"ask_question"}), threads, MagicMock(), monkeypatch)
    response = client.post(f"/api/v1/threads/{thread}/rerun-sql", json={"logical_sql": "SELECT 1"})
    assert response.status_code == 403


def test_feedback_on_a_foreign_message_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = InMemoryThreadStore()
    thread = _thread(threads, owner=OTHER)
    message_id = threads.append(thread, OTHER, PROJECT, Turn("q", "q", "SELECT 1", None))
    client = _client(_context({"give_feedback"}), threads, MagicMock(), monkeypatch)
    response = client.post(f"/api/v1/messages/{message_id}/feedback", json={"rating": "up"})
    assert response.status_code == 404


# ------------------------------------------------------------------ chat history


def test_a_failed_question_is_saved_but_does_not_feed_follow_ups() -> None:
    threads = InMemoryThreadStore()
    svc = service(ScriptedLlm(sql("DROP TABLE orders")), RecordingAudit())
    svc._threads = threads  # type: ignore[assignment]
    principal = build_principal("u1", {"region": "West"})
    list(svc.stream("p", 1, MDL, principal, build_policies(), "bad one", thread_id="t", contract=True))
    (saved,) = threads.messages("t")
    assert saved.turn.failure_code == "NON_SELECT" and saved.turn.logical_sql is None
    assert threads.history("t", "u1", limit=10) == []  # a failure is not context for the next turn


def test_a_successful_turn_keeps_timing_and_suggestions_with_it() -> None:
    threads = InMemoryThreadStore()
    svc = service(ScriptedLlm(sql("SELECT order_id FROM orders")), RecordingAudit())
    svc._threads = threads  # type: ignore[assignment]
    principal = build_principal("u1", {"region": "West"})
    events = list(
        svc.stream("p", 1, MDL, principal, build_policies(), "orders?", thread_id="t", contract=True)
    )
    (saved,) = threads.messages("t")
    assert saved.turn.failure_code is None and saved.turn.latency_ms is not None
    assert saved.turn.logical_sql == "SELECT order_id FROM orders"
    assert events[-1].data["message_id"] == saved.id


def test_the_conversation_list_shows_only_the_callers_threads_with_the_first_question() -> None:
    threads = InMemoryThreadStore()
    threads.append("a", "u1", "p", Turn("first question", "first question"))
    threads.append("a", "u1", "p", Turn("second", "second"))
    threads.append("b", "u2", "p", Turn("someone else", "someone else"))
    threads.create("empty", "p", "u1")  # created but never asked: not listed
    items = threads.list_threads("p", "u1", 10)
    assert [(t.id, t.title, t.turns) for t in items] == [("a", "first question", 2)]


def test_list_threads_route_and_thread_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = InMemoryThreadStore()
    threads.append("t1", OWNER, PROJECT, Turn("q", "q", "SELECT 1", "ans", summary="Sum", latency_ms=5))
    threads.append("t1", OWNER, PROJECT, Turn("bad", "bad", failure_code="TIMEOUT", failure_message="slow"))
    client = _client(_context({"ask_question"}), threads, MagicMock(), monkeypatch)
    listing = client.get(f"/api/v1/projects/{PROJECT}/threads").json()["items"]
    assert [(i["id"], i["title"], i["turns"]) for i in listing] == [("t1", "q", 2)]
    # the route needs a UUID id; the in-memory store takes any string, so read it back directly
    assert threads.messages("t1")[1].turn.failure_code == "TIMEOUT"


def test_running_a_message_is_owner_only(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = InMemoryThreadStore()
    message_id = threads.append("t1", OTHER, PROJECT, Turn("q", "q", "SELECT 1"))
    client = _client(_context({"ask_question"}), threads, MagicMock(), monkeypatch)
    assert client.post(f"/api/v1/messages/{message_id}/run").status_code == 404


def test_a_turn_saves_the_step_timeline_and_models_the_ui_draws() -> None:
    threads = InMemoryThreadStore()
    svc = service(ScriptedLlm(sql("SELECT order_id FROM orders")), RecordingAudit())
    svc._threads = threads  # type: ignore[assignment]
    principal = build_principal("u1", {"region": "West"})
    list(svc.stream("p", 7, MDL, principal, build_policies(), "orders?", thread_id="t", contract=True))
    (saved,) = threads.messages("t")
    assert saved.turn.model_version == 7
    trace = saved.turn.trace
    assert trace is not None and trace["models"]
    assert [s["title"] for s in trace["steps"]] == [
        "Intent", "Retrieval", "SQL candidate", "Compilation", "Validation", "PostgreSQL",
    ]
    assert all(isinstance(s["ms"], int) and s["ok"] for s in trace["steps"])


def test_a_correction_shows_up_as_a_failed_step_in_the_saved_timeline() -> None:
    threads = InMemoryThreadStore()
    llm = ScriptedLlm(sql("SELECT order_dt FROM orders"), sql("SELECT order_id FROM orders"))
    svc = service(llm, RecordingAudit())
    svc._threads = threads  # type: ignore[assignment]
    principal = build_principal("u1", {"region": "West"})
    list(svc.stream("p", 1, MDL, principal, build_policies(), "orders?", thread_id="t", contract=True))
    steps = threads.messages("t")[0].turn.trace["steps"]  # type: ignore[index]
    titles = [(s["title"], s["ok"]) for s in steps]
    assert ("Compilation", False) in titles and ("Correction", False) in titles
