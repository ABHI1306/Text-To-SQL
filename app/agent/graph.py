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

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import ValidationError
from sqlglot import exp, parse_one

from app.agent.guard import safe_reply, screen_question
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
# Retries may differ from the first try (which stays at 0.0): the same prompt at 0.0 tends to
# give the same wrong answer again. One entry per correction attempt, the last one repeats.
CORRECTION_TEMPERATURES = (0.3, 0.5, 0.7)

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
    """Everything the graph nodes need: the deployed model, policies, ports and limits."""
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


def build_graph(
    deps: AgentDeps, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Wire the nodes into the question graph; every query path runs compile, validate, execute.

    With a ``checkpointer`` an unclear question pauses the run (human in the loop): the user's
    next message resumes it. Without one, an unclear question ends the turn with its question.
    """
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(AgentState)

    small = deps.small_llm or deps.llm
    model_names = [model.name for model in deps.mdl.models]

    def rewrite_followup(state: AgentState) -> dict[str, Any]:
        """Make a follow-up standalone using thread history; an LLM error is a typed failure."""
        history = state.get("history") or []
        question = state["question"]
        refusal = screen_question(question)  # the raw text, before a rewrite can soften it
        if refusal is not None:
            return {"standalone_question": question, "intent": "respond", "reply": refusal}
        if not history:
            return {"standalone_question": question}
        try:
            rewritten = rewrite_question(small, question, history, model_names)
            if screen_question(rewritten) is not None:
                rewritten = question  # the rewrite turned an ordinary question into a probe
            if _lost_the_users_words(question, rewritten):
                return {
                    "standalone_question": rewritten,
                    "intent": "clarify",
                    "reply": f'I read this as: "{rewritten}". Is that what you meant?',
                    "options": [rewritten],
                }
            return {"standalone_question": rewritten}
        except Exception:  # noqa: BLE001 - fail closed into a typed failure
            return {"failure": _llm_down()}

    def classify_intent(state: AgentState) -> dict[str, Any]:
        """Route the question to data question, clarify or respond with the small model."""
        if state.get("intent") in {"respond", "clarify"} and state.get("reply"):
            return {}  # already settled by the guard or the rewrite check; no model call
        try:
            intent, reply = route_intent(
                small, state["standalone_question"], [model.name for model in deps.mdl.models]
            )
        except Exception:  # noqa: BLE001 - fail closed into a typed failure
            return {"failure": _llm_down()}
        options = _clarify_options(deps.mdl) if intent == "clarify" else []
        return {"intent": intent, "reply": safe_reply(reply), "options": options}

    def clarify(state: AgentState) -> dict[str, Any]:
        """Ask the user and pause; their answer resumes here and the question is classified again.

        Without a checkpointer this is terminal: the clarifying question is already in ``reply``.
        """
        if checkpointer is None:
            return {}
        answer = str(
            interrupt({"question": state.get("reply") or "", "options": state.get("options") or []})
        ).strip()
        refusal = screen_question(answer)
        if refusal is not None:
            return {"intent": "respond", "reply": refusal, "options": []}
        original = state.get("standalone_question") or state["question"]
        try:
            merged = rewrite_question(small, answer, [(original, None)], model_names)
        except Exception:  # noqa: BLE001 - fail closed into a typed failure
            return {"failure": _llm_down()}
        if screen_question(merged) is not None:
            merged = answer  # the merge turned the answer into a probe
        return {"standalone_question": merged, "intent": "", "reply": None, "options": []}

    def respond(state: AgentState) -> dict[str, Any]:
        """Terminal node: the chat reply is already in ``reply``."""
        return {}

    def retrieve_context(state: AgentState) -> dict[str, Any]:
        """Retrieve relevant models and knowledge; prune the model to what the prompt may show."""
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
        """Ask the SQL model for a logical query over the pruned models."""
        prompt = build_generation_prompt(
            state["standalone_question"], state["context_text"], state.get("knowledge_text", "")
        )
        return _candidate(deps.llm, prompt)

    def compile_sql(state: AgentState) -> dict[str, Any]:
        """Compile logical SQL with the caller's row policies; an unexpected error fails closed."""
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
        errors = list(compiled.errors)
        if not errors and _returns_only_constants(state.get("logical_sql") or ""):
            errors.append(
                CompileError(
                    CompileErrorCode.SYNTAX_ERROR,
                    "SELECT must return columns or aggregates from a model, not only constants",
                )
            )
        return {
            "sql": compiled.sql or None,
            "parameters": dict(compiled.parameters),
            "policy_context": compiled.policy_context or None,
            "missing_attributes": compiled.missing_attributes,
            "validation_errors": errors,
        }

    def validate_sql(state: AgentState) -> dict[str, Any]:
        """Allowlist-check the compiled SQL, then dry-run it with EXPLAIN.

        Only the SQLSTATE reaches the model, never the physical-object message; an unreachable
        database ends the run instead of spending correction attempts.
        """
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
            if _database_unreachable(error, getattr(error, "sqlstate", None)):
                # retrying the model cannot help; say so instead of spending correction attempts
                return {"failure": classify_execution_error(error), "validation_errors": []}
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
        """Ask the model to fix the rejected query, given the typed errors and allowed names."""
        prompt = build_correction_prompt(
            state["standalone_question"],
            state["context_text"],
            state.get("logical_sql") or "",
            state["validation_errors"],
            state["allowed_names"],
            state.get("knowledge_text", ""),
        )
        attempt = state.get("attempts", 0)
        temperature = CORRECTION_TEMPERATURES[min(attempt, len(CORRECTION_TEMPERATURES) - 1)]
        return {
            **_candidate(deps.llm, prompt, temperature),
            "attempts": attempt + 1,
            "last_errors": _error_signature(state["validation_errors"]),
        }

    def execute_sql(state: AgentState) -> dict[str, Any]:
        """Run the validated query read-only with the row limit; errors become typed failures."""
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
        """Optional grounded summary of the rows; any problem just skips it."""
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
        """Optional chart spec, kept only if it passes ``validate_chart_spec``."""
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
        """Terminal failure node: keep an existing failure or build one from the first error."""
        if state.get("failure"):
            return {}
        errors = tuple(state.get("validation_errors") or ())
        first = errors[0] if errors else None
        detail = first.message if first else "The query could not be validated."
        return {
            "failure": StructuredFailure(
                first.code.value if first else "VALIDATION_FAILED",
                f"I couldn't build a reliable query for this question ({detail}). "
                "Try asking it in a different way.",
                retryable=False,
                errors=errors,
            )
        }

    def retry_or_fail(state: AgentState) -> Literal["correct_sql", "structured_failure"]:
        """Correct again while attempts remain and the last correction changed something."""
        if _error_signature(state.get("validation_errors") or []) == state.get("last_errors"):
            return "structured_failure"  # the retry made the same mistake: more retries waste time
        if state.get("attempts", 0) < deps.max_corrections:
            return "correct_sql"
        return "structured_failure"

    def after_rewrite(state: AgentState) -> Literal["classify_intent", "structured_failure"]:
        """Route after rewrite: stop on failure, else classify."""
        return "structured_failure" if state.get("failure") else "classify_intent"

    def after_classify(
        state: AgentState,
    ) -> Literal["retrieve_context", "clarify", "respond", "structured_failure"]:
        """Route by intent: clarify, respond, or retrieve for a data question."""
        if state.get("failure"):
            return "structured_failure"
        intent = state.get("intent")
        if intent == "clarify":
            return "clarify"
        if intent == "respond":
            return "respond"
        return "retrieve_context"

    def after_clarify(
        state: AgentState,
    ) -> Literal["classify_intent", "respond", "structured_failure", "__end__"]:
        """After the user answers: classify the merged question again; a refusal is shown."""
        if state.get("failure"):
            return "structured_failure"
        if state.get("intent") == "respond":
            return "respond"
        if state.get("intent") == "clarify":
            return "__end__"  # no checkpointer: the clarifying question ends the turn
        return "classify_intent"

    def after_retrieve(state: AgentState) -> Literal["generate_sql", "structured_failure"]:
        """Route after retrieval: stop on failure, else generate."""
        return "structured_failure" if state.get("failure") else "generate_sql"

    def after_generate(state: AgentState) -> Literal["compile_sql", "structured_failure"]:
        """Route after generation: stop on failure, else compile."""
        return "structured_failure" if state.get("failure") else "compile_sql"

    def after_compile(
        state: AgentState,
    ) -> Literal["validate_sql", "correct_sql", "structured_failure"]:
        """Route after compile: validate when clean, else correct or fail."""
        if not state.get("validation_errors"):
            return "validate_sql"
        return retry_or_fail(state)

    def after_validate(
        state: AgentState,
    ) -> Literal["execute_sql", "correct_sql", "structured_failure"]:
        """Route after validation: execute when clean, else correct or fail."""
        if state.get("failure"):
            return "structured_failure"
        if not state.get("validation_errors"):
            return "execute_sql"
        return retry_or_fail(state)

    def after_execute(state: AgentState) -> Literal["structured_failure", "summarize"]:
        """Route after execution: stop on failure, else summarize."""
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
    graph.add_conditional_edges("clarify", after_clarify)
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
    return graph.compile(checkpointer=checkpointer)


_FILLER_WORDS = frozenset(
    "the a an and or of to in on for with by from is are was were do does did we you me my our"
    " show give list tell what which who how many much only just please can could would about"
    " each every all any some there here have has had get got into out than then more most"
    " less ones one".split()
)


def _returns_only_constants(logical_sql: str) -> bool:
    """True when the query reads no real table, so any rows it returns are text the model made
    up (a UNION of literals, a subquery of literals, a constant SELECT) shown as data."""
    try:
        root = parse_one(logical_sql, read="postgres")
    except Exception:  # noqa: BLE001 - unparsable SQL is the compiler's error to report
        return False
    ctes = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
    if not any(table.name.lower() not in ctes or table.db for table in root.find_all(exp.Table)):
        return True
    return all(_is_literal_select(branch) for branch in _branches(root))


def _branches(root: exp.Expr) -> list[exp.Expr]:
    """The SELECTs a query returns rows from: itself, or both sides of each UNION."""
    if isinstance(root, exp.SetOperation):
        return _branches(root.this) + _branches(root.expression)
    return [root]


def _is_literal_select(node: exp.Expr) -> bool:
    """A SELECT whose every returned column is a literal."""
    return isinstance(node, exp.Select) and all(
        isinstance(item.unalias(), (exp.Literal, exp.Null, exp.Boolean))
        for item in node.expressions
    )


def _stem(word: str) -> str:
    """A crude prefix stem so 'closed' and 'close' count as the same word."""
    return word[:5]


def _lost_the_users_words(original: str, rewritten: str) -> bool:
    """True when a rewrite dropped most of what the user actually typed, so it should be
    confirmed with them instead of run. Faithful rewrites add context and keep the user's words."""
    words = [
        w for w in re.findall(r"[a-z0-9]+", original.lower())
        if len(w) > 3 and w not in _FILLER_WORDS and not _REFERENCE_WORDS.fullmatch(w)
    ]
    if not words or original.strip().lower() == rewritten.strip().lower():
        return False
    kept = {_stem(w) for w in re.findall(r"[a-z0-9]+", rewritten.lower())}
    return sum(_stem(w) in kept for w in words) / len(words) < 0.5


def _clarify_options(mdl: Mdl) -> list[str]:
    """Up to three ready-made questions built from the model names, one click each."""
    names = [model.name.replace("_", " ") for model in mdl.models][:3]
    return [f"How many {name} do we have?" for name in names]


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


def is_self_contained(message: str, model_names: Sequence[str] = ()) -> bool:
    """True when the message names its own subject and refers to nothing earlier.

    Naming one of the project's models ("show me customers") is enough of a subject even in
    fewer than four words.

    Such a message is a new question: it is used as written, so a period or filter from the
    previous turn can never leak into it and no model call is spent on it.
    """
    text = message.strip()
    names_a_model = _names_a_model(text, model_names)
    return (
        (len(text.split()) >= _MIN_STANDALONE_WORDS or names_a_model)
        and _FRAGMENT_START.search(text) is None
        and _REFERENCE_WORDS.search(text) is None
    )


def _names_a_model(text: str, model_names: Sequence[str]) -> bool:
    """True when a word of the message matches a word of a model name ('customer' ~ customers)."""
    wanted = {_stem(part) for name in model_names for part in name.lower().split("_") if part}
    return any(_stem(word) in wanted for word in re.findall(r"[a-z0-9]+", text.lower()) if len(word) > 3)


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
    llm: LLMPort,
    question: str,
    history: Sequence[tuple[str, str | None]],
    model_names: Sequence[str] = (),
) -> str:
    """The message as a standalone question; the raw message when the rewrite is unusable.

    Raises when the model itself is unavailable (the graph turns that into a typed failure).
    """
    if is_self_contained(question, model_names):
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
    """The retryable failure used when the local model cannot be reached."""
    return StructuredFailure(
        "LLM_UNAVAILABLE", "The local language model is unavailable.", retryable=True
    )


def _error_signature(errors: Sequence[CompileError]) -> tuple[tuple[str, str], ...]:
    """What went wrong, ignoring order: two equal signatures mean the retry changed nothing."""
    return tuple(sorted((error.code.value, error.identifier or error.message) for error in errors))


def _candidate(llm: LLMPort, prompt: str, temperature: float = 0.0) -> dict[str, Any]:
    """One structured call, with one repair retry on unparseable output before it counts."""
    try:
        raw = llm.generate(prompt, temperature=temperature, schema=SQL_CANDIDATE_SCHEMA)
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
    """The model's reply as an ``SQLCandidate``, or None when it is not valid."""
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
    if _database_unreachable(error, sqlstate):
        return StructuredFailure(
            "DATABASE_UNAVAILABLE", "The database is not reachable right now. Try again shortly.", True
        )
    return StructuredFailure("DRY_RUN_FAILED", "The database could not run the query.")


_UNREACHABLE_ERRORS = frozenset(
    {"OperationalError", "InterfaceError", "PoolTimeout", "ConnectionTimeout", "PoolClosed"}
)
# connection exceptions (class 08), shutdown in progress (57P01-57P03), too many connections
_UNREACHABLE_SQLSTATES = frozenset({"57P01", "57P02", "57P03", "53300"})


def _database_unreachable(error: Exception, sqlstate: object) -> bool:
    """True when the server could not be reached, as opposed to refusing or failing the query."""
    if isinstance(error, (ConnectionError, TimeoutError)):
        return True
    if isinstance(sqlstate, str) and (sqlstate.startswith("08") or sqlstate in _UNREACHABLE_SQLSTATES):
        return True
    # a driver error without an SQLSTATE never got an answer from the server
    return sqlstate is None and type(error).__name__ in _UNREACHABLE_ERRORS


def _usable_pairs(retrieval: RetrievalResult, mdl: Mdl) -> list[SqlPair]:
    """Approved pairs that compile against the models shown to the model, so an example never
    teaches a name that is not under MODELS (or one the deployed model has since dropped)."""
    return [pair for pair in retrieval.pairs if _compiles(pair.logical_sql, mdl)]


def _compiles(logical_sql: str, mdl: Mdl) -> bool:
    """True when an approved example compiles against the model; errors just exclude it."""
    try:
        return compile_query(logical_sql, mdl, None).ok
    except Exception:  # noqa: BLE001 - an unusable example is just left out
        return False


def _prune(mdl: Mdl, retrieval: RetrievalResult) -> Mdl:
    """Copy of the model limited to the retrieved models, columns, relationships and metrics."""
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
    """Names the model may use, grouped as models, columns per model and metrics."""
    names = {"models": [model.name for model in mdl.models]}
    for model in mdl.models:
        names[f"columns of {model.name}"] = [column.name for column in model.columns]
    if mdl.metrics:
        names["metrics"] = [metric.name for metric in mdl.metrics]
    return names
