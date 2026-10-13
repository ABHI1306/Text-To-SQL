"""The question graph: retrieve, generate, compile, validate, execute, with typed correction.

The only route to execution is compile_sql -> validate_sql -> execute_sql, and a query is
corrected at most ``max_corrections`` times before it fails visibly.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import ValidationError

from app.agent.prompts import (
    SQL_CANDIDATE_SCHEMA,
    SQLCandidate,
    build_correction_prompt,
    build_generation_prompt,
    build_repair_prompt,
)
from app.agent.state import AgentState, StructuredFailure
from app.core.types import ValidatedQuery
from app.domain.mdl import Mdl
from app.domain.policy import RowPolicy
from app.ports.llm import LLMPort
from app.ports.warehouse import WarehousePort
from app.semantic.compiler import CompileError, CompileErrorCode, compile_query
from app.semantic.prompt_context import render_prompt_context
from app.semantic.retrieval import RetrievalResult, RetrievalService
from app.semantic.validator import validate_compiled_sql

MAX_CORRECTIONS = 3

NODES = (
    "retrieve_context",
    "generate_sql",
    "compile_sql",
    "validate_sql",
    "correct_sql",
    "execute_sql",
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


def build_graph(deps: AgentDeps) -> CompiledStateGraph[Any, Any, Any, Any]:
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(AgentState)

    def retrieve_context(state: AgentState) -> dict[str, Any]:
        try:
            retrieval = deps.retrieval.retrieve(
                state["project_id"],
                str(deps.version),
                state["question"],
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
        return {
            "context": retrieval,
            "context_text": render_prompt_context(pruned),
            "allowed_names": _allowed_names(pruned),
            "attempts": 0,
            "validation_errors": [],
        }

    def generate_sql(state: AgentState) -> dict[str, Any]:
        prompt = build_generation_prompt(state["question"], state["context_text"])
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
            state["question"],
            state["context_text"],
            state.get("logical_sql") or "",
            state["validation_errors"],
            state["allowed_names"],
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

    def after_execute(state: AgentState) -> Literal["structured_failure", "__end__"]:
        return "structured_failure" if state.get("failure") else "__end__"

    graph.add_node("retrieve_context", retrieve_context)
    graph.add_node("generate_sql", generate_sql)
    graph.add_node("compile_sql", compile_sql)
    graph.add_node("validate_sql", validate_sql)
    graph.add_node("correct_sql", correct_sql)
    graph.add_node("execute_sql", execute_sql)
    graph.add_node("structured_failure", structured_failure)
    graph.add_edge(START, "retrieve_context")
    graph.add_conditional_edges("retrieve_context", after_retrieve)
    graph.add_conditional_edges("generate_sql", after_generate)
    graph.add_conditional_edges("compile_sql", after_compile)
    graph.add_conditional_edges("validate_sql", after_validate)
    graph.add_conditional_edges("correct_sql", after_generate)
    graph.add_conditional_edges("execute_sql", after_execute)
    graph.add_edge("structured_failure", END)
    return graph.compile()


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
