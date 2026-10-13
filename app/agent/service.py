"""Run the question graph and turn its node updates into the ordered SSE events."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.agent.graph import AgentDeps, build_graph
from app.agent.prompts import PROMPT_VERSION
from app.agent.state import StructuredFailure
from app.audit.service import AuditService
from app.core.types import JsonValue, Principal
from app.domain.mdl import Mdl
from app.domain.policy import RowPolicy
from app.ports.embedder import EmbedderPort
from app.ports.llm import LLMPort
from app.ports.vector_store import VectorStorePort
from app.ports.warehouse import WarehousePort
from app.semantic.compiler import CompileError
from app.semantic.retrieval import RetrievalService


@dataclass(frozen=True)
class AskEvent:
    type: str
    data: dict[str, Any]


@dataclass(frozen=True)
class AskResult:
    ok: bool
    logical_sql: str | None
    sql: str | None
    columns: list[str]
    rows: list[list[JsonValue]]
    truncated: bool
    retrieved_models: list[str]
    policy_context: str | None
    attempts: int
    elapsed_ms: int
    failure: StructuredFailure | None
    events: list[AskEvent] = field(default_factory=list)


class AskService:
    """The only entry point from a question to rows: every path goes through the graph."""

    def __init__(
        self,
        embedder: EmbedderPort,
        vector_store: VectorStorePort,
        llm: LLMPort,
        warehouse: WarehousePort,
        *,
        audit: AuditService | None = None,
        max_rows: int = 1000,
    ) -> None:
        self._retrieval = RetrievalService(embedder, vector_store)
        self._llm = llm
        self._warehouse = warehouse
        self._audit = audit
        self._max_rows = max_rows

    def stream(
        self,
        project_id: str,
        version: int,
        mdl: Mdl,
        principal: Principal,
        policies: Sequence[RowPolicy],
        question: str,
        *,
        include_compiled: bool = False,
        thread_id: str | None = None,
        capture: dict[str, Any] | None = None,
    ) -> Iterator[AskEvent]:
        if not question.strip():
            raise ValueError("Question cannot be empty")
        graph = build_graph(
            AgentDeps(
                mdl, version, policies, self._retrieval, self._llm, self._warehouse, self._max_rows
            )
        )
        started = last = time.monotonic()
        state: dict[str, Any] = {}
        for update in graph.stream(
            {
                "project_id": project_id,
                "thread_id": thread_id or str(uuid.uuid4()),
                "question": question.strip(),
                "principal": principal,
            },
            stream_mode="updates",
        ):
            for node, change in update.items():
                state.update(change or {})
                now = time.monotonic()
                for event in _events(node, change or {}, state, include_compiled):
                    yield AskEvent(event.type, {**event.data, "ms": round((now - last) * 1000)})
                last = now
        elapsed = round((time.monotonic() - started) * 1000)
        failure: StructuredFailure | None = state.get("failure")
        if capture is not None:
            capture.update(state)
            capture["elapsed_ms"] = elapsed
        self._audit_question(principal, project_id, question, state, failure)
        if failure is not None:
            yield AskEvent(
                "error",
                {"code": failure.code, "message": failure.message, "retryable": failure.retryable},
            )
        else:
            yield AskEvent(
                "done",
                {
                    "message_id": str(uuid.uuid4()),
                    "latency_ms": elapsed,
                    "policy_context": state.get("policy_context"),
                    "attempts": state.get("attempts", 0),
                    "prompt_version": PROMPT_VERSION,
                },
            )

    def ask(
        self,
        project_id: str,
        version: int,
        mdl: Mdl,
        principal: Principal,
        policies: Sequence[RowPolicy],
        question: str,
        *,
        include_compiled: bool = False,
    ) -> AskResult:
        state: dict[str, Any] = {}
        events = list(
            self.stream(
                project_id, version, mdl, principal, policies, question,
                include_compiled=include_compiled, capture=state,
            )
        )
        by_type = {event.type: event for event in events}
        rows = by_type.get("rows")
        retrieval = by_type.get("retrieval")
        candidates = [e for e in events if e.type == "sql_candidate"]
        compilations = [e for e in events if e.type == "compilation" and e.data.get("sql")]
        failure: StructuredFailure | None = state.get("failure")
        return AskResult(
            ok=failure is None,
            logical_sql=candidates[-1].data["logical_sql"] if candidates else None,
            sql=compilations[-1].data["sql"] if compilations else None,
            columns=list(rows.data["columns"]) if rows else [],
            rows=list(rows.data["rows"]) if rows else [],
            truncated=bool(rows.data["truncated"]) if rows else False,
            retrieved_models=list(retrieval.data["models"]) if retrieval else [],
            policy_context=state.get("policy_context") if failure is None else None,
            attempts=int(state.get("attempts", 0)),
            elapsed_ms=int(state.get("elapsed_ms", 0)),
            failure=failure,
            events=events,
        )

    def _audit_question(
        self,
        principal: Principal,
        project_id: str,
        question: str,
        state: Mapping[str, Any],
        failure: StructuredFailure | None,
    ) -> None:
        if self._audit is None:
            return
        self._audit.record(
            principal.user_id,
            "question_asked",
            "project",
            project_id,
            {
                "question": question[:500],
                "ok": failure is None,
                "failure_code": failure.code if failure else None,
                "policy_context": state.get("policy_context"),
                "attempts": state.get("attempts", 0),
                "row_count": len(state.get("rows") or []),
            },
        )
        missing = state.get("missing_attributes") or ()
        if missing:
            self._audit.record(
                principal.user_id,
                "row_policy_missing_attribute",
                "project",
                project_id,
                {"attributes": list(missing), "policy_context": state.get("policy_context")},
            )


def _issues(errors: Sequence[CompileError]) -> list[JsonValue]:
    return [
        {
            "code": error.code.value,
            "message": error.message,
            "identifier": error.identifier,
            "suggestions": list(error.suggestions),
        }
        for error in errors
    ]


def _events(
    node: str, change: Mapping[str, Any], state: Mapping[str, Any], include_compiled: bool
) -> Iterator[AskEvent]:
    if node == "retrieve_context" and "context" in change:
        yield AskEvent(
            "retrieval",
            {
                "models": [model.name for model in change["context"].models],
                "pairs": [],
                "instructions": [],
            },
        )
    elif node in {"generate_sql", "correct_sql"} and "logical_sql" in change:
        attempts = int(state.get("attempts", 0))
        if node == "correct_sql":
            errors = state.get("validation_errors") or []
            yield AskEvent(
                "correction",
                {"attempt": attempts, "reason": errors[0].message if errors else "rejected"},
            )
        yield AskEvent(
            "sql_candidate", {"logical_sql": change["logical_sql"], "attempt": attempts + 1}
        )
    elif node == "compile_sql":
        errors = change.get("validation_errors") or []
        data: dict[str, JsonValue] = {"ok": not errors, "errors": _issues(errors)}
        if include_compiled and not errors:
            data["sql"] = change.get("sql")
        yield AskEvent("compilation", data)
    elif node == "validate_sql":
        errors = change.get("validation_errors") or []
        yield AskEvent("validation", {"ok": not errors, "errors": _issues(errors)})
    elif node == "execute_sql" and "rows" in change:
        rows = [[_json_value(cell) for cell in row] for row in change["rows"]]
        yield AskEvent(
            "rows",
            {
                "columns": list(change["columns"]),
                "rows": rows,
                "truncated": bool(change.get("truncated")),
                "row_count": len(rows),
            },
        )


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)
