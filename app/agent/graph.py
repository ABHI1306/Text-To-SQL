"""The question graph: retrieve, generate, compile, validate, execute, with typed correction.

The only route to execution is compile_sql -> validate_sql -> execute_sql, and a query is
corrected at most ``max_corrections`` times before it fails visibly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import ValidationError

from app.agent.presentation import summary_is_grounded, validate_chart_spec
from app.agent.prompts import (
    CHART_SCHEMA,
    INTENT_SCHEMA,
    REWRITE_SCHEMA,
    SQL_CANDIDATE_SCHEMA,
    SUMMARY_SCHEMA,
    ChartDecision,
    IntentDecision,
    RewrittenQuestion,
    SQLCandidate,
    SummaryDecision,
    build_chart_prompt,
    build_correction_prompt,
    build_generation_prompt,
    build_intent_prompt,
    build_repair_prompt,
    build_rewrite_prompt,
    build_summary_prompt,
    render_knowledge,
)
from app.agent.state import AgentState, StructuredFailure
from app.core.types import ValidatedQuery
from app.domain.mdl import Mdl
from app.domain.policy import RowPolicy
from app.ports.knowledge import SqlPair
from app.ports.llm import LLMPort
from app.ports.warehouse import WarehousePort
from app.semantic.compiler import CompileError, CompileErrorCode, compile_query
from app.semantic.prompt_context import render_prompt_context
from app.semantic.retrieval import RetrievalResult, RetrievalService
from app.semantic.validator import validate_compiled_sql

MAX_CORRECTIONS = 3

NODES = (
    "rewrite_followup",
    "classify_intent",
    "clarify",
    "respond",
    "retrieve_context",
    "generate_sql",
    "compile_sql",
    "validate_sql",
    "correct_sql",
    "execute_sql",
    "summarize",
    "chart",
    "structured_failure",
)


@dataclass(frozen=True)
class AgentDeps:
    mdl: Mdl
    version: int
    policies: Sequence[RowPolicy]
    retrieval: RetrievalService
    llm: LLMPort
    warehouse: WarehousePort
    max_rows: int = 1000
    max_corrections: int = MAX_CORRECTIONS
    small_llm: LLMPort | None = None  # routing model; the SQL model when not given
    summary_llm: LLMPort | None = None  # summary and chart model; both are skipped when None


def build_graph(deps: AgentDeps) -> CompiledStateGraph[Any, Any, Any, Any]:
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(AgentState)

    small = deps.small_llm or deps.llm

    def rewrite_followup(state: AgentState) -> dict[str, Any]:
        history = state.get("history") or []
        question = state["question"]
        if not history:
            return {"standalone_question": question}
        try:
            return {"standalone_question": rewrite_question(small, question, history)}
        except Exception:  # noqa: BLE001 - fail closed into a typed failure
            return {"failure": _llm_down()}

    def classify_intent(state: AgentState) -> dict[str, Any]:
        try:
            intent, reply = route_intent(
                small, state["standalone_question"], [model.name for model in deps.mdl.models]
            )
        except Exception:  # noqa: BLE001 - fail closed into a typed failure
            return {"failure": _llm_down()}
        return {"intent": intent, "reply": reply}

    def clarify(state: AgentState) -> dict[str, Any]:
        return {}

    def respond(state: AgentState) -> dict[str, Any]:
        return {}

    def retrieve_context(state: AgentState) -> dict[str, Any]:
        try:
            retrieval = deps.retrieval.retrieve(
                state["project_id"],
                str(deps.version),
                state["standalone_question"],
                deps.mdl,
                limit=5,
                max_columns_per_model=24,
            )
        except Exception:  # noqa: BLE001 - fail closed into a typed failure
            return {
                "failure": StructuredFailure(
                    "LLM_UNAVAILABLE", "Retrieval is unavailable", retryable=True
                )
            }
        if not retrieval.models:
            return {
                "failure": StructuredFailure(
                    "OUT_OF_SCOPE",
                    "No relevant deployed models were found for this question.",
                )
            }
        pruned = _prune(deps.mdl, retrieval)
        pairs = _usable_pairs(retrieval, pruned)
        instructions = [i for i in retrieval.instructions if i.text.strip()]
        return {
            "context": retrieval,
            "context_text": render_prompt_context(pruned),
            "knowledge_text": render_knowledge(
                [i.text for i in instructions], [(p.question, p.logical_sql) for p in pairs]
            ),
            "knowledge_pairs": [{"id": p.id, "question": p.question} for p in pairs],
            "knowledge_instructions": [{"id": i.id, "text": i.text} for i in instructions],
            "allowed_names": _allowed_names(pruned),
            "attempts": 0,
            "validation_errors": [],
        }

    def generate_sql(state: AgentState) -> dict[str, Any]:
        prompt = build_generation_prompt(
            state["standalone_question"], state["context_text"], state.get("knowledge_text", "")
        )
        return _candidate(deps.llm, prompt)

    def compile_sql(state: AgentState) -> dict[str, Any]:
        try:
            compiled = compile_query(
                state.get("logical_sql") or "", deps.mdl, state["principal"], deps.policies
            )
        except Exception:  # noqa: BLE001 - an unexpected compiler error must fail closed, typed
            return {
                "sql": None,
                "parameters": {},
                "policy_context": None,
                "missing_attributes": (),
                "validation_errors": [
                    CompileError(CompileErrorCode.SYNTAX_ERROR, "the query could not be compiled")
                ],
            }
        return {
            "sql": compiled.sql or None,
            "parameters": dict(compiled.parameters),
            "policy_context": compiled.policy_context or None,
            "missing_attributes": compiled.missing_attributes,
            "validation_errors": list(compiled.errors),
        }

    def validate_sql(state: AgentState) -> dict[str, Any]:
        sql = state["sql"] or ""
        try:
            result = validate_compiled_sql(sql, deps.mdl)
        except Exception:  # noqa: BLE001 - fail closed, typed
            return {
                "validation_errors": [
                    CompileError(CompileErrorCode.SYNTAX_ERROR, "the compiled query could not be validated")
                ]
            }
        if not result.ok:
            return {"validation_errors": list(result.errors)}
        try:
            deps.warehouse.explain(
                ValidatedQuery(sql=sql, parameters=state.get("parameters") or {})
            )
        except Exception as error:  # noqa: BLE001 - fail closed into a typed failure
            # The PostgreSQL message names physical objects, so the model only gets the code.
            return {
                "validation_errors": [
                    CompileError(
                        CompileErrorCode.DRY_RUN_FAILED,
                        "PostgreSQL could not plan the compiled query"
                        f" (SQLSTATE {getattr(error, 'sqlstate', None) or 'unknown'}).",
                    )
                ]
            }
        return {"validation_errors": []}

    def correct_sql(state: AgentState) -> dict[str, Any]:
        prompt = build_correction_prompt(
            state["standalone_question"],
            state["context_text"],
            state.get("logical_sql") or "",
            state["validation_errors"],
            state["allowed_names"],
            state.get("knowledge_text", ""),
        )
        return {**_candidate(deps.llm, prompt), "attempts": state.get("attempts", 0) + 1}

    def execute_sql(state: AgentState) -> dict[str, Any]:
        try:
            result = deps.warehouse.execute_read_only(
                ValidatedQuery(sql=state["sql"] or "", parameters=state.get("parameters") or {}),
                max_rows=deps.max_rows,
            )
        except Exception as error:  # noqa: BLE001 - fail closed into a typed failure
            return {"failure": classify_execution_error(error)}
        return {
            "columns": list(result.columns),
            "rows": [list(row) for row in result.rows],
            "truncated": result.truncated,
        }

    def summarize(state: AgentState) -> dict[str, Any]:
        if deps.summary_llm is None or not state.get("rows"):
            return {}
        try:
            raw = deps.summary_llm.generate(
                build_summary_prompt(
                    state["standalone_question"], state["columns"], state["rows"]
                ),
                temperature=0.0,
                schema=SUMMARY_SCHEMA,
            )
            text = SummaryDecision.model_validate(json.loads(raw)).summary.strip()
            if text and summary_is_grounded(
                text, state["standalone_question"], state["columns"], state["rows"],
                row_limit=deps.max_rows,
            ):
                return {"summary": text[:1000]}
        except Exception:  # noqa: BLE001 - a summary is optional; the rows are already answered
            return {}
        return {}

    def chart(state: AgentState) -> dict[str, Any]:
        if deps.summary_llm is None or len(state.get("rows") or []) < 2:
            return {}
        try:
            raw = deps.summary_llm.generate(
                build_chart_prompt(state["standalone_question"], state["columns"], state["rows"]),
                temperature=0.0,
                schema=CHART_SCHEMA,
            )
            proposed = ChartDecision.model_validate(json.loads(raw)).chart
            if proposed is None:
                return {}
            checked = validate_chart_spec(proposed, state["columns"], state["rows"])
            if checked.spec is not None:
                return {"chart": checked.spec}
        except Exception:  # noqa: BLE001 - a chart is optional; the rows are already answered
            return {}
        return {}

    def structured_failure(state: AgentState) -> dict[str, Any]:
        if state.get("failure"):
            return {}
        errors = tuple(state.get("validation_errors") or ())
        first = errors[0] if errors else None
        return {
            "failure": StructuredFailure(
                first.code.value if first else "VALIDATION_FAILED",
                first.message if first else "The query could not be validated.",
                retryable=False,
                errors=errors,
            )
        }

    def retry_or_fail(state: AgentState) -> Literal["correct_sql", "structured_failure"]:
        if state.get("attempts", 0) < deps.max_corrections:
            return "correct_sql"
        return "structured_failure"

    def after_rewrite(state: AgentState) -> Literal["classify_intent", "structured_failure"]:
        return "structured_failure" if state.get("failure") else "classify_intent"

    def after_classify(
        state: AgentState,
    ) -> Literal["retrieve_context", "clarify", "respond", "structured_failure"]:
        if state.get("failure"):
            return "structured_failure"
        intent = state.get("intent")
        if intent == "clarify":
            return "clarify"
        if intent == "respond":
            return "respond"
        return "retrieve_context"

    def after_retrieve(state: AgentState) -> Literal["generate_sql", "structured_failure"]:
        return "structured_failure" if state.get("failure") else "generate_sql"

    def after_generate(state: AgentState) -> Literal["compile_sql", "structured_failure"]:
        return "structured_failure" if state.get("failure") else "compile_sql"

    def after_compile(
        state: AgentState,
    ) -> Literal["validate_sql", "correct_sql", "structured_failure"]:
        if not state.get("validation_errors"):
            return "validate_sql"
        return retry_or_fail(state)

    def after_validate(
        state: AgentState,
    ) -> Literal["execute_sql", "correct_sql", "structured_failure"]:
        if not state.get("validation_errors"):
            return "execute_sql"
        return retry_or_fail(state)

    def after_execute(state: AgentState) -> Literal["structured_failure", "summarize"]:
        return "structured_failure" if state.get("failure") else "summarize"

    graph.add_node("rewrite_followup", rewrite_followup)
    graph.add_node("classify_intent", classify_intent)
    graph.add_node("clarify", clarify)
    graph.add_node("respond", respond)
    graph.add_node("retrieve_context", retrieve_context)
    graph.add_node("generate_sql", generate_sql)
    graph.add_node("compile_sql", compile_sql)
    graph.add_node("validate_sql", validate_sql)
    graph.add_node("correct_sql", correct_sql)
    graph.add_node("execute_sql", execute_sql)
    graph.add_node("summarize", summarize)
    graph.add_node("chart", chart)
    graph.add_node("structured_failure", structured_failure)
    graph.add_edge(START, "rewrite_followup")
    graph.add_conditional_edges("rewrite_followup", after_rewrite)
    graph.add_conditional_edges("classify_intent", after_classify)
    graph.add_edge("clarify", END)
    graph.add_edge("respond", END)
    graph.add_conditional_edges("retrieve_context", after_retrieve)
    graph.add_conditional_edges("generate_sql", after_generate)
    graph.add_conditional_edges("compile_sql", after_compile)
    graph.add_conditional_edges("validate_sql", after_validate)
    graph.add_conditional_edges("correct_sql", after_generate)
    graph.add_conditional_edges("execute_sql", after_execute)
    graph.add_edge("summarize", "chart")
    graph.add_edge("chart", END)
    graph.add_edge("structured_failure", END)
    return graph.compile()


_REFERENCE_WORDS = re.compile(
    r"\b(it|its|they|them|their|theirs|those|these|that|this|he|she|his|her|same|former|latter"
    r"|above|previous|earlier|also|too|instead|other|others|rest|remaining)\b",
    re.IGNORECASE,
)
# A fragment or a sentence with no subject of its own: it only makes sense after the previous turn.
_FRAGMENT_START = re.compile(
    r"^\W*(and|but|or|also|what about|how about|only|just|except|excluding|including|now|then"
    r"|same|instead|(?:how many|which|what|who) (?:are|is|were|was|do|does|did))\b",
    re.IGNORECASE,
)
_MIN_STANDALONE_WORDS = 4


def is_self_contained(message: str) -> bool:
    """True when the message names its own subject and refers to nothing earlier.

    Such a message is a new question: it is used as written, so a period or filter from the
    previous turn can never leak into it and no model call is spent on it.
    """
    text = message.strip()
    return (
        len(text.split()) >= _MIN_STANDALONE_WORDS
        and _FRAGMENT_START.search(text) is None
        and _REFERENCE_WORDS.search(text) is None
    )


def recent_context(history: Sequence[tuple[str, str | None]]) -> Sequence[tuple[str, str | None]]:
    """The turns a follow-up can refer to: the latest data question and whatever came after it.

    Stored questions are already standalone, so older turns add nothing and only tempt the model
    to bind a pronoun to the wrong subject. Chit-chat after the data question is kept because
    "sort that" still points at the data question before it.
    """
    for index in range(len(history) - 1, -1, -1):
        if history[index][1] is not None:
            return history[index:]
    return history[-1:]


def rewrite_question(
    llm: LLMPort, question: str, history: Sequence[tuple[str, str | None]]
) -> str:
    """The message as a standalone question; the raw message when the rewrite is unusable.

    Raises when the model itself is unavailable (the graph turns that into a typed failure).
    """
    if is_self_contained(question):
        return question
    raw = llm.generate(
        build_rewrite_prompt(question, recent_context(history)),
        temperature=0.0,
        schema=REWRITE_SCHEMA,
    )
    try:
        rewritten = RewrittenQuestion.model_validate(json.loads(raw)).question.strip()
    except (ValueError, ValidationError):
        rewritten = ""
    # An unusable rewrite falls back to the raw message: the compiler gates still apply.
    return rewritten or question


def route_intent(
    llm: LLMPort, question: str, model_names: Sequence[str]
) -> tuple[str, str | None]:
    """(intent, reply). Unreadable routing, or a clarify/respond without text, is a data question
    (which still faces every gate). Raises when the model is unavailable."""
    raw = llm.generate(
        build_intent_prompt(question, model_names), temperature=0.0, schema=INTENT_SCHEMA
    )
    try:
        decision = IntentDecision.model_validate(json.loads(raw))
    except (ValueError, ValidationError):
        return "data_question", None
    reply = decision.message.strip() or None
    if decision.intent != "data_question" and reply is None:
        return "data_question", None
    return decision.intent, reply


def _llm_down() -> StructuredFailure:
    return StructuredFailure(
        "LLM_UNAVAILABLE", "The local language model is unavailable.", retryable=True
    )


def _candidate(llm: LLMPort, prompt: str) -> dict[str, Any]:
    """One structured call, with one repair retry on unparseable output before it counts."""
    try:
        raw = llm.generate(prompt, temperature=0.0, schema=SQL_CANDIDATE_SCHEMA)
        parsed = _parse(raw)
        if parsed is None:
            raw = llm.generate(
                build_repair_prompt(raw), temperature=0.0, schema=SQL_CANDIDATE_SCHEMA
            )
            parsed = _parse(raw)
    except Exception:  # noqa: BLE001 - fail closed into a typed failure
        return {
            "failure": StructuredFailure(
                "LLM_UNAVAILABLE", "The local language model is unavailable.", retryable=True
            )
        }
    return {"logical_sql": parsed.logical_sql.strip() if parsed else ""}


def _parse(raw: str) -> SQLCandidate | None:
    try:
        return SQLCandidate.model_validate(json.loads(raw))
    except (ValueError, ValidationError):
        return None


def classify_execution_error(error: Exception) -> StructuredFailure:
    """Map a driver or port error to a typed failure without naming physical objects."""
    sqlstate = getattr(error, "sqlstate", None)
    if sqlstate == "57014" or type(error).__name__ == "PortTimeout":
        return StructuredFailure("TIMEOUT", "The query took too long and was stopped.", True)
    if sqlstate == "42501":
        return StructuredFailure("FORBIDDEN", "The database refused this query.")
    return StructuredFailure("DRY_RUN_FAILED", "The database could not run the query.")


def _usable_pairs(retrieval: RetrievalResult, mdl: Mdl) -> list[SqlPair]:
    """Approved pairs that compile against the models shown to the model, so an example never
    teaches a name that is not under MODELS (or one the deployed model has since dropped)."""
    return [pair for pair in retrieval.pairs if _compiles(pair.logical_sql, mdl)]


def _compiles(logical_sql: str, mdl: Mdl) -> bool:
    try:
        return compile_query(logical_sql, mdl, None).ok
    except Exception:  # noqa: BLE001 - an unusable example is just left out
        return False


def _prune(mdl: Mdl, retrieval: RetrievalResult) -> Mdl:
    selected = {model.name: model for model in retrieval.models}
    models = [
        model.model_copy(update={"columns": list(selected[model.name].columns)})
        for model in mdl.models
        if model.name in selected
    ]
    return mdl.model_copy(
        update={
            "models": models,
            "relationships": list(retrieval.relationships),
            "metrics": [m for m in mdl.metrics if m.base_object in selected],
        }
    )


def _allowed_names(mdl: Mdl) -> dict[str, list[str]]:
    names = {"models": [model.name for model in mdl.models]}
    for model in mdl.models:
        names[f"columns of {model.name}"] = [column.name for column in model.columns]
    if mdl.metrics:
        names["metrics"] = [metric.name for metric in mdl.metrics]
    return names
