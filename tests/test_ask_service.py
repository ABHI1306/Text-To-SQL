"""AskService: event contract, audit trail and result shape around the graph."""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.service import AskService
from app.semantic.retrieval import RetrievalResult, RetrievedModel
from tests.support.audit import RecordingAudit
from tests.support.ecommerce_mdl import build_mdl, build_policies, build_principal
from tests.test_agent_graph import FakeWarehouse, RoutingLlm, ScriptedLlm, sql

MDL = build_mdl()


class StaticEmbedder:
    def embed(self, texts: Any) -> list[tuple[float, ...]]:
        return [(1.0,) for _ in texts]


class StaticStore:
    def search(self, *_: Any, **__: Any) -> list[Any]:
        return []


def service(llm: Any, audit: RecordingAudit, warehouse: FakeWarehouse | None = None) -> AskService:
    svc = AskService(StaticEmbedder(), StaticStore(), llm, warehouse or FakeWarehouse(), audit=audit, small_llm=RoutingLlm())  # type: ignore[arg-type]

    class Retrieval:
        def retrieve(self, *_: Any, **__: Any) -> RetrievalResult:
            models = tuple(
                RetrievedModel(m.name, m.description, m.table_reference, tuple(m.columns), 1.0)
                for m in MDL.models
            )
            return RetrievalResult(models, tuple(MDL.relationships), ())

    svc._retrieval = Retrieval()  # type: ignore[assignment]
    return svc


def ask(svc: AskService, question: str = "orders?", attributes: dict[str, Any] | None = None, **kw: Any):  # type: ignore[no-untyped-def]
    principal = build_principal("u1", {"region": "West"} if attributes is None else attributes)
    return svc.ask("p", 1, MDL, principal, build_policies(), question, **kw)


def test_events_follow_the_contract_and_carry_milliseconds() -> None:
    audit = RecordingAudit()
    result = ask(service(ScriptedLlm(sql("SELECT order_id FROM orders")), audit))
    assert [e.type for e in result.events] == [
        "retrieval", "sql_candidate", "compilation", "validation", "rows", "done",
    ]
    assert all(isinstance(e.data["ms"], int) for e in result.events[:-1])
    assert result.ok and result.logical_sql == "SELECT order_id FROM orders"
    assert result.retrieved_models and result.rows == [[1], [2]]


def test_compiled_sql_is_withheld_unless_the_account_may_see_it() -> None:
    audit = RecordingAudit()
    svc = service(ScriptedLlm(sql("SELECT order_id FROM orders")), audit)
    hidden = ask(svc)
    shown = ask(svc, include_compiled=True)
    assert hidden.sql is None and all("sql" not in e.data for e in hidden.events if e.type == "compilation")
    assert shown.sql and "m_orders" in shown.sql


def test_a_correction_is_visible_as_extra_events_not_hidden() -> None:
    llm = ScriptedLlm(sql("SELECT order_dt FROM orders"), sql("SELECT order_id FROM orders"))
    result = ask(service(llm, RecordingAudit()))
    types = [e.type for e in result.events]
    assert types.count("sql_candidate") == 2 and types.count("correction") == 1
    assert types.index("correction") < types.index("rows") and result.attempts == 1


def test_a_failure_ends_in_one_error_event_and_no_rows() -> None:
    result = ask(service(ScriptedLlm(sql("DROP TABLE orders")), RecordingAudit()))
    assert not result.ok and result.failure is not None and result.failure.code == "NON_SELECT"
    assert result.events[-1].type == "error" and "rows" not in {e.type for e in result.events}


def test_every_question_is_audited_with_its_policy_context_and_outcome() -> None:
    audit = RecordingAudit()
    ask(service(ScriptedLlm(sql("SELECT order_id FROM orders")), audit))
    ask(service(ScriptedLlm(sql("DROP TABLE orders")), audit))
    ok, failed = audit.events
    assert ok["action"] == failed["action"] == "question_asked" and ok["actor_id"] == "u1"
    assert ok["details"]["ok"] is True and ok["details"]["policy_context"] and ok["details"]["row_count"] == 2
    assert failed["details"]["ok"] is False and failed["details"]["failure_code"] == "NON_SELECT"


def test_a_missing_attribute_writes_its_own_audit_event() -> None:
    audit = RecordingAudit()
    result = ask(service(ScriptedLlm(sql("SELECT order_id FROM orders")), audit), attributes={})
    assert audit.actions() == ["question_asked", "row_policy_missing_attribute"]
    assert audit.events[1]["details"]["attributes"] == ["region"] and result.ok


def test_an_empty_question_is_refused_before_anything_runs() -> None:
    audit = RecordingAudit()
    with pytest.raises(ValueError, match="empty"):
        ask(service(ScriptedLlm(sql("SELECT 1")), audit), "   ")
    assert audit.events == []


def test_a_thread_remembers_turns_and_feeds_them_to_the_rewrite() -> None:
    from app.adapters.metadata.threads import InMemoryThreadStore

    threads = InMemoryThreadStore()
    small = RoutingLlm(rewrite="how many orders in 2024")
    svc = service(ScriptedLlm(sql("SELECT order_id FROM orders")), RecordingAudit())
    svc._small_llm, svc._threads = small, threads  # type: ignore[assignment]

    first = ask(svc, "how many orders")
    second = ask(svc, "and in 2024?", thread_id=first.thread_id)

    assert second.events[0].type == "rewrite"
    assert second.events[0].data["question"] == "how many orders in 2024"
    turns = threads.history(first.thread_id, "u1", limit=10)
    assert [t.question for t in turns] == ["how many orders", "and in 2024?"]
    assert "SELECT order_id FROM orders" in small.prompts[-2]  # history reached the rewrite


def test_another_users_thread_id_gives_no_history_and_cannot_be_written() -> None:
    from app.adapters.metadata.threads import InMemoryThreadStore

    threads = InMemoryThreadStore()
    svc = service(ScriptedLlm(sql("SELECT order_id FROM orders")), RecordingAudit())
    svc._threads = threads  # type: ignore[assignment]
    first = ask(svc, "how many orders")
    intruder = build_principal("u2", {"region": "West"})
    with pytest.raises(PermissionError):
        svc.ask("p", 1, MDL, intruder, build_policies(), "x", thread_id=first.thread_id)
