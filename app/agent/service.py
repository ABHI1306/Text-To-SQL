"""Run the question graph and turn its node updates into the ordered SSE events."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.agent.graph import AgentDeps, build_graph
from app.agent.prompts import PROMPT_VERSION
from app.agent.state import StructuredFailure
from app.audit.service import AuditService
from app.core.jsonable import json_value as _json_value
from app.core.types import JsonValue, Principal
from app.domain.mdl import Mdl
from app.domain.policy import RowPolicy
from app.observability.usage import METRICS, Usage, bind_usage
from app.ports.embedder import EmbedderPort
from app.ports.knowledge import KnowledgeLookup
from app.ports.llm import LLMPort
from app.ports.threads import ThreadStorePort, Turn
from app.ports.vector_store import VectorStorePort
from app.ports.warehouse import WarehousePort
from app.semantic.compiler import CompileError
from app.semantic.retrieval import RetrievalCache, RetrievalService
from app.semantic.runner import compile_for, execute_compiled, failure_from


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
    thread_id: str = ""
    intent: str = "data_question"
    reply: str | None = None
    summary: str | None = None
    chart: dict[str, Any] | None = None
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
        small_llm: LLMPort | None = None,
        threads: ThreadStorePort | None = None,
        summary_llm: LLMPort | None = None,
        knowledge: KnowledgeLookup | None = None,
        retrieval_cache: RetrievalCache | None = None,
    ) -> None:
        self._retrieval = RetrievalService(
            embedder, vector_store, knowledge=knowledge, cache=retrieval_cache
        )
        self._summary_llm = summary_llm
        self._llm = llm
        self._warehouse = warehouse
        self._audit = audit
        self._max_rows = max_rows
        self._small_llm = small_llm
        self._threads = threads

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
        max_rows: int | None = None,
        history_user_id: str | None = None,
        remember: bool = True,
        contract: bool = False,
    ) -> Iterator[AskEvent]:
        """Run the graph and yield events in order.

        ``contract`` switches to the section 8 SSE vocabulary (intent, answer_token, suggestions);
        ``max_rows`` can only lower the configured limit; ``history_user_id`` and ``remember``
        let a preview run read the thread owner's history without writing to it.
        """
        if not question.strip():
            raise ValueError("Question cannot be empty")
        row_limit = self._max_rows if max_rows is None else max(1, min(max_rows, self._max_rows))
        graph = build_graph(
            AgentDeps(
                mdl, version, policies, self._retrieval, self._llm, self._warehouse, row_limit,
                small_llm=self._small_llm, summary_llm=self._summary_llm,
            )
        )
        thread_id = thread_id or str(uuid.uuid4())
        turns = (
            self._threads.history(
                thread_id, history_user_id or principal.user_id, limit=HISTORY_TURNS
            )
            if self._threads
            else []
        )
        started = last = time.monotonic()
        history = [(t.standalone_question, t.logical_sql) for t in turns]
        state: dict[str, Any] = {"history": history}
        seen: list[AskEvent] = []
        usage = Usage()
        updates = iter(
            graph.stream(
                {
                    "project_id": project_id,
                    "thread_id": thread_id,
                    "question": question.strip(),
                    "history": history,
                    "principal": principal,
                },
                stream_mode="updates",
            )
        )
        while True:
            with bind_usage(usage):
                update = next(updates, None)
            if update is None:
                break
            for node, change in update.items():
                state.update(change or {})
                now = time.monotonic()
                for event in _events(node, change or {}, state, include_compiled):
                    if event.type == "classify" and not contract:
                        continue
                    if contract:
                        mapped = _contract_event(event, state)
                        if mapped is None:
                            continue
                        event = mapped
                    timed = AskEvent(event.type, {**event.data, "ms": round((now - last) * 1000)})
                    seen.append(timed)
                    yield timed
                last = now
        elapsed = round((time.monotonic() - started) * 1000)
        failure: StructuredFailure | None = state.get("failure")
        if capture is not None:
            capture.update(state)
            capture["thread_id"] = thread_id
            capture["elapsed_ms"] = elapsed
        self._audit_question(principal, project_id, question, state, failure)
        suggestions: list[str] = []
        if contract and failure is None and state.get("intent") == "data_question" \
                and state.get("rows") is not None:
            with bind_usage(usage):
                suggestions = self._suggest(state)
            if suggestions:
                yield AskEvent("suggestions", {"items": suggestions})
        METRICS.add_question(usage, elapsed)
        message_id = None
        if remember:
            message_id = self._remember(
                thread_id, principal, project_id, question, state, failure, elapsed, suggestions,
                trace_from_events(seen), version,
            )
        if failure is not None:
            yield AskEvent(
                "error",
                {"code": failure.code, "message": failure.message, "retryable": failure.retryable},
            )
        else:
            yield AskEvent(
                "done",
                {
                    "thread_id": thread_id,
                    "message_id": message_id or str(uuid.uuid4()),
                    "model_version": version,
                    "latency_ms": elapsed,
                    "tokens": usage.tokens,
                    "gpu_ms": usage.gpu_ms if usage.calls else None,
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
        thread_id: str | None = None,
    ) -> AskResult:
        state: dict[str, Any] = {}
        events = list(
            self.stream(
                project_id, version, mdl, principal, policies, question,
                include_compiled=include_compiled, thread_id=thread_id, capture=state,
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
            thread_id=str(state.get("thread_id") or ""),
            intent=str(state.get("intent") or "data_question"),
            reply=state.get("reply"),
            summary=state.get("summary"),
            chart=state.get("chart"),
            events=events,
        )

    def _suggest(self, state: Mapping[str, Any]) -> list[str]:
        """Up to three follow-up questions from the small model; never fails the answer."""
        llm = self._small_llm or self._summary_llm
        if llm is None:
            return []
        try:
            raw = llm.generate(
                build_suggestions_prompt(
                    str(state.get("standalone_question") or ""),
                    list(state.get("columns") or []),
                ),
                temperature=0.2,
                schema=SUGGESTIONS_SCHEMA,
            )
            items = json.loads(raw).get("items", [])
        except Exception:  # noqa: BLE001 - suggestions are optional
            return []
        return [str(item).strip()[:200] for item in items if str(item).strip()][:3]

    def rerun(
        self,
        project_id: str,
        version: int,
        mdl: Mdl,
        principal: Principal,
        policies: Sequence[RowPolicy],
        logical_sql: str,
        *,
        max_rows: int | None = None,
    ) -> tuple[list[AskEvent], StructuredFailure | None]:
        """Run edited logical SQL through the same compiler, validator and limits as a question."""
        row_limit = self._max_rows if max_rows is None else max(1, min(max_rows, self._max_rows))
        events: list[AskEvent] = []
        started = time.monotonic()
        compiled = compile_for(logical_sql, mdl, principal, policies)
        policy_context: str | None = None
        if isinstance(compiled, tuple):
            events.append(AskEvent("compilation", {"ok": False, "errors": _issues(compiled)}))
            failure: StructuredFailure | None = failure_from(compiled)
        else:
            policy_context = compiled.policy_context or None
            events.append(AskEvent("compilation", {"ok": True, "errors": [], "sql": compiled.sql}))
            outcome = execute_compiled(compiled, mdl, self._warehouse, row_limit)
            failure = outcome.failure
            events.append(
                AskEvent(
                    "validation",
                    {"ok": not outcome.validation_errors, "errors": _issues(outcome.validation_errors)},
                )
            )
            if failure is None:
                rows = [[_json_value(cell) for cell in row] for row in outcome.rows]
                events.append(
                    AskEvent(
                        "rows",
                        {
                            "columns": outcome.columns,
                            "rows": rows,
                            "truncated": outcome.truncated,
                            "row_count": len(rows),
                        },
                    )
                )
        if self._audit is not None:
            self._audit.record(
                principal.user_id,
                "sql_rerun",
                "project",
                project_id,
                {
                    "logical_sql": logical_sql[:2000],
                    "ok": failure is None,
                    "failure_code": failure.code if failure else None,
                    "policy_context": policy_context,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                },
            )
        return events, failure

    def _remember(
        self, thread_id: str, principal: Principal, project_id: str, question: str,
        state: Mapping[str, Any], failure: StructuredFailure | None, elapsed_ms: int,
        suggestions: Sequence[str], trace: dict[str, Any] | None = None,
        model_version: int | None = None,
    ) -> str | None:
        """Save the turn, failed ones too. Rows are never stored (see migration 0008)."""
        if self._threads is None:
            return None
        data_question = state.get("intent") == "data_question"
        return self._threads.append(
            thread_id, principal.user_id, project_id,
            Turn(
                question[:2000],
                str(state.get("standalone_question") or question)[:2000],
                state.get("logical_sql") if data_question and failure is None else None,
                state.get("reply"),
                summary=state.get("summary"),
                chart=state.get("chart"),
                suggestions=tuple(suggestions),
                latency_ms=elapsed_ms,
                failure_code=failure.code if failure else None,
                failure_message=failure.message[:500] if failure else None,
                trace=trace,
                model_version=model_version,
            ),
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


HISTORY_TURNS = 6

SUGGESTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "string"}, "maxItems": 3}},
    "required": ["items"],
}


def build_suggestions_prompt(question: str, columns: Sequence[str]) -> str:
    return (
        "A user just asked a data question and got an answer. Suggest up to 3 short follow-up"
        " questions they might ask next, using only these result columns for ideas.\n"
        f"Question: {question}\nColumns: {', '.join(columns)}\n"
        'Return JSON only, exactly {"items": ["...", "..."]}.'
    )


def _issue_text(errors: object) -> str:
    items = errors if isinstance(errors, list) else []
    return "; ".join(
        f"E_{item.get('code')} {item.get('message', '')}".strip()
        for item in items if isinstance(item, dict)
    )


def trace_from_events(events: Sequence[AskEvent]) -> dict[str, Any]:
    """The step timeline of a run in the form the UI draws it (one entry per step event)."""
    steps: list[dict[str, Any]] = []
    models: list[str] = []

    def add(title: str, detail: str, ok: bool, event: AskEvent) -> None:
        steps.append({"title": title, "detail": detail, "ok": ok, "ms": int(event.data.get("ms", 0))})

    for event in events:
        data = event.data
        if event.type in {"intent", "classify"}:
            follow_up = data.get("used_context") is True
            add("Intent", f"Follow-up, read as: {data.get('standalone_question', '')}" if follow_up
                else str(data.get("intent", "")).replace("_", " "), True, event)
        elif event.type == "retrieval":
            models = [str(m) for m in data.get("models", [])]
            parts = [f"{len(models)} models", f"{len(data.get('pairs', []))} SQL pairs",
                     f"{len(data.get('instructions', []))} instructions"]
            add("Retrieval", ", ".join(parts), True, event)
        elif event.type == "sql_candidate":
            add("SQL candidate", f"Attempt {data.get('attempt', 1)}", True, event)
        elif event.type == "correction":
            add("Correction", f"Attempt {data.get('attempt', '')}: {data.get('reason', '')}", False, event)
        elif event.type == "compilation":
            ok = data.get("ok") is True
            add("Compilation", "Row policies applied" if ok else _issue_text(data.get("errors")), ok, event)
        elif event.type == "validation":
            ok = data.get("ok") is True
            add("Validation", "Single read-only query; dry run passed" if ok
                else _issue_text(data.get("errors")), ok, event)
        elif event.type == "rows":
            count = int(data.get("row_count", 0))
            add("PostgreSQL", f"{count} row{'' if count == 1 else 's'} returned"
                + (" (results capped)" if data.get("truncated") else ""), True, event)
    return {"steps": steps, "models": models}


def _contract_event(event: AskEvent, state: Mapping[str, Any]) -> AskEvent | None:
    """Translate an internal event to the section 8 SSE contract; None drops it."""
    if event.type == "rewrite":
        return None  # folded into the intent event
    if event.type == "classify":
        return AskEvent("intent", event.data)
    if event.type in {"answer", "summary"}:
        text = event.data.get("message") if event.type == "answer" else event.data.get("text")
        return AskEvent("answer_token", {"text": text or ""})
    if event.type == "chart":
        return AskEvent("chart", {"vega_lite_spec": event.data.get("spec")})
    if event.type == "clarification":
        return AskEvent("clarification", {"question": event.data.get("message") or "", "options": []})
    return event


def _events(
    node: str, change: Mapping[str, Any], state: Mapping[str, Any], include_compiled: bool
) -> Iterator[AskEvent]:
    if node == "rewrite_followup" and state.get("history"):
        yield AskEvent("rewrite", {"question": change.get("standalone_question", "")})
    elif node == "classify_intent" and change.get("intent"):
        yield AskEvent(
            "classify",
            {
                "intent": change["intent"],
                "standalone_question": state.get("standalone_question", ""),
                "used_context": bool(state.get("history")),
            },
        )
    elif node == "clarify":
        yield AskEvent("clarification", {"message": state.get("reply")})
    elif node == "respond":
        yield AskEvent("answer", {"message": state.get("reply")})
    elif node == "retrieve_context" and "context" in change:
        yield AskEvent(
            "retrieval",
            {
                "models": [model.name for model in change["context"].models],
                "pairs": list(state.get("knowledge_pairs") or []),
                "instructions": list(state.get("knowledge_instructions") or []),
            },
        )
    elif node == "summarize" and change.get("summary"):
        yield AskEvent("summary", {"text": change["summary"]})
    elif node == "chart" and change.get("chart"):
        yield AskEvent("chart", {"spec": change["chart"]})
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
