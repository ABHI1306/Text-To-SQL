"""State carried through the question graph, and the typed failure that ends a run."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, TypedDict

from app.core.types import JsonValue, Principal
from app.semantic.compiler import CompileError
from app.semantic.retrieval import RetrievalResult


@dataclass(frozen=True)
class StructuredFailure:
    """Terminal, typed failure: the only way a question ends without rows."""

    code: str
    message: str
    retryable: bool = False
    errors: tuple[CompileError, ...] = field(default_factory=tuple)


class AgentState(TypedDict, total=False):
    """The graph's shared state; each node returns only the keys it changes."""
    project_id: str
    thread_id: str
    principal: Principal
    question: str
    history: list[tuple[str, str | None]]
    standalone_question: str
    intent: str
    reply: str | None
    context: RetrievalResult
    context_text: str
    knowledge_text: str
    knowledge_pairs: list[dict[str, str]]
    knowledge_instructions: list[dict[str, str]]
    allowed_names: dict[str, list[str]]
    logical_sql: str | None
    sql: str | None
    parameters: Mapping[str, JsonValue]
    policy_context: str | None
    missing_attributes: tuple[str, ...]
    validation_errors: list[CompileError]
    attempts: int
    columns: list[str]
    rows: list[list[object]]
    truncated: bool
    summary: str | None
    chart: dict[str, Any] | None
    failure: StructuredFailure | None
