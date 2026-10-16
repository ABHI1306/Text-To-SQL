"""Run one logical SQL statement under one principal: compile, validate, plan, execute.

Every non-agent path to rows (rerun-sql, tiles, scheduled questions) goes through here, so the
same compiler, row policies, validator and limits apply as for a question.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.agent.graph import classify_execution_error
from app.agent.state import StructuredFailure
from app.core.types import Principal, ValidatedQuery
from app.domain.mdl import Mdl
from app.domain.policy import RowPolicy
from app.ports.warehouse import WarehousePort
from app.semantic.compiler import CompiledQuery, CompileError, CompileErrorCode, compile_query
from app.semantic.validator import validate_compiled_sql


@dataclass(frozen=True)
class LogicalRun:
    failure: StructuredFailure | None
    compilation_errors: tuple[CompileError, ...] = ()
    validation_errors: tuple[CompileError, ...] = ()
    sql: str | None = None
    policy_context: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list[object]] = field(default_factory=list)
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.failure is None


def failure_from(errors: Sequence[CompileError]) -> StructuredFailure:
    first = errors[0] if errors else None
    return StructuredFailure(
        first.code.value if first else "VALIDATION_FAILED",
        first.message if first else "The query could not be validated.",
        retryable=False,
        errors=tuple(errors),
    )


def compile_for(
    logical_sql: str, mdl: Mdl, principal: Principal, policies: Sequence[RowPolicy]
) -> CompiledQuery | tuple[CompileError, ...]:
    """The compiled query, or the errors; an unexpected compiler error fails closed."""
    try:
        compiled = compile_query(logical_sql, mdl, principal, policies)
    except Exception:  # noqa: BLE001 - fail closed, typed
        return (CompileError(CompileErrorCode.SYNTAX_ERROR, "the query could not be compiled"),)
    return compiled if compiled.ok else tuple(compiled.errors)


def execute_compiled(
    compiled: CompiledQuery, mdl: Mdl, warehouse: WarehousePort, max_rows: int
) -> LogicalRun:
    checked = validate_compiled_sql(compiled.sql, mdl)
    if not checked.ok:
        return LogicalRun(
            failure_from(checked.errors),
            validation_errors=tuple(checked.errors),
            sql=compiled.sql,
            policy_context=compiled.policy_context or None,
        )
    query = ValidatedQuery(sql=compiled.sql, parameters=dict(compiled.parameters))
    try:
        warehouse.explain(query)
        result = warehouse.execute_read_only(query, max_rows=max_rows)
    except Exception as error:  # noqa: BLE001 - fail closed into a typed failure
        return LogicalRun(
            classify_execution_error(error),
            sql=compiled.sql,
            policy_context=compiled.policy_context or None,
        )
    return LogicalRun(
        None,
        sql=compiled.sql,
        policy_context=compiled.policy_context or None,
        columns=list(result.columns),
        rows=[list(row) for row in result.rows],
        truncated=result.truncated,
    )


def run_logical_sql(
    logical_sql: str,
    mdl: Mdl,
    principal: Principal,
    policies: Sequence[RowPolicy],
    warehouse: WarehousePort,
    max_rows: int,
) -> LogicalRun:
    compiled = compile_for(logical_sql, mdl, principal, policies)
    if isinstance(compiled, tuple):
        return LogicalRun(failure_from(compiled), compilation_errors=compiled)
    return execute_compiled(compiled, mdl, warehouse, max_rows)

