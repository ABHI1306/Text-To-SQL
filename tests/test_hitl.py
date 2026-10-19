"""Human in the loop: an unclear question pauses the run and the user's answer resumes it."""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agent.graph import AgentDeps, build_graph
from app.agent.guard import REFUSAL_WRITE
from app.agent.service import checkpoint_serializer
from tests.support.ecommerce_mdl import build_mdl, build_policies, build_principal
from tests.test_agent_graph import FakeRetrieval, FakeWarehouse, RoutingLlm, ScriptedLlm, sql


class _AskThenRoute(RoutingLlm):
    """Unclear on the first look; once the user's answer is merged in, a data question."""

    def generate(self, prompt: str, **kwargs: Any) -> str:
        if "<history>" in prompt:  # the merge of the user's answer with their first question
            return '{"question": "How many orders were placed last month?"}'
        if "placed last month" in prompt:
            return '{"intent": "data_question", "message": ""}'
        return '{"intent": "clarify", "message": "Which metric and period do you mean?"}'


def _graph(small: RoutingLlm, warehouse: FakeWarehouse) -> Any:
    deps = AgentDeps(
        build_mdl(), 1, build_policies(), FakeRetrieval(),
        ScriptedLlm(sql("SELECT order_id FROM orders")), warehouse, small_llm=small,
    )
    return build_graph(deps, InMemorySaver(serde=checkpoint_serializer()))


def _start(question: str) -> dict[str, Any]:
    return {
        "project_id": "p", "thread_id": "t", "question": question, "history": [],
        "principal": build_principal("west", {"region": "West"}),
    }


CONFIG: RunnableConfig = {"configurable": {"thread_id": "run-1"}}


def test_an_unclear_question_pauses_before_touching_the_database() -> None:
    warehouse = FakeWarehouse()
    graph = _graph(_AskThenRoute(), warehouse)
    graph.invoke(_start("show me stuff"), CONFIG)
    snapshot = graph.get_state(CONFIG)
    assert snapshot.next == ("clarify",)
    (pending,) = snapshot.tasks[0].interrupts
    assert pending.value["question"] == "Which metric and period do you mean?"
    assert warehouse.executed == []


def test_the_users_answer_resumes_the_same_run_and_answers_the_question() -> None:
    warehouse = FakeWarehouse()
    graph = _graph(_AskThenRoute(), warehouse)
    graph.invoke(_start("show me stuff"), CONFIG)
    state = graph.invoke(Command(resume="last month"), CONFIG)
    assert state["standalone_question"] == "How many orders were placed last month?"
    assert state["intent"] == "data_question" and state.get("failure") is None
    assert len(warehouse.executed) == 1
    assert graph.get_state(CONFIG).next == ()


def test_a_hostile_answer_to_the_clarification_is_refused() -> None:
    warehouse = FakeWarehouse()
    graph = _graph(_AskThenRoute(), warehouse)
    graph.invoke(_start("show me stuff"), CONFIG)
    state = graph.invoke(Command(resume="delete the customers table"), CONFIG)
    assert state["reply"] == REFUSAL_WRITE and warehouse.executed == []


def test_an_answer_that_is_still_unclear_pauses_again() -> None:
    class StillUnclear(RoutingLlm):
        def generate(self, prompt: str, **kwargs: Any) -> str:
            if "<history>" in prompt:
                return '{"question": "stuff about things"}'
            return '{"intent": "clarify", "message": "Can you be more specific?"}'

    warehouse = FakeWarehouse()
    graph = _graph(StillUnclear(), warehouse)
    graph.invoke(_start("show me stuff"), CONFIG)
    graph.invoke(Command(resume="things"), CONFIG)
    assert graph.get_state(CONFIG).next == ("clarify",) and warehouse.executed == []


def test_the_service_pauses_on_an_unclear_question_and_resumes_on_the_next_message() -> None:
    from tests.support.audit import RecordingAudit
    from tests.test_ask_service import ask, service

    warehouse = FakeWarehouse()
    svc = service(ScriptedLlm(sql("SELECT order_id FROM orders")), RecordingAudit(), warehouse)
    svc._small_llm = _AskThenRoute()

    first = ask(svc, "show me stuff", thread_id="conversation-1")
    types = [e.type for e in first.events]
    assert "clarification" in types and "rows" not in types and warehouse.executed == []
    assert first.intent == "clarify" and first.reply == "Which metric and period do you mean?"

    second = ask(svc, "last month", thread_id="conversation-1")
    assert second.ok and warehouse.executed and second.intent == "data_question"
    assert [e.type for e in second.events].count("clarification") == 0
    assert svc._paused == {}


def test_a_different_conversation_is_not_resumed_by_another_ones_answer() -> None:
    from tests.support.audit import RecordingAudit
    from tests.test_ask_service import ask, service

    warehouse = FakeWarehouse()
    svc = service(ScriptedLlm(sql("SELECT order_id FROM orders")), RecordingAudit(), warehouse)
    svc._small_llm = _AskThenRoute()
    ask(svc, "show me stuff", thread_id="conversation-1")
    ask(svc, "orders last month", thread_id="conversation-2")  # its own run, not a resume
    assert set(svc._paused) == {"conversation-1", "conversation-2"}
    assert svc._paused["conversation-1"] != svc._paused["conversation-2"]
    assert warehouse.executed == []
