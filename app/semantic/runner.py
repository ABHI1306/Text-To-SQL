"""Run one logical SQL statement under one principal: compile, validate, plan, execute.

Every non-agent path to rows (rerun-sql, tiles, scheduled questions) goes through here, so the
same compiler, row policies, validator and limits apply as for a question.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from threading import Lock

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
    """Result of running logical SQL: rows on success, otherwise a typed failure."""
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
        """True when the run had no failure."""
        return self.failure is None


def failure_from(errors: Sequence[CompileError]) -> StructuredFailure:
    """Structured failure for the first of ``errors``."""
    first = errors[0] if errors else None
    return StructuredFailure(
        first.code.value if first else "VALIDATION_FAILED",
        first.message if first else "The query could not be validated.",
        retryable=False,
        errors=tuple(errors),
    )


class CompileCache:
    """Compiled output reused for the same question text, model, policies and principal.

    The key is a hash of everything the compiler reads (the logical SQL, the whole model, every
    enabled policy, and the principal's id, role and attributes), so one user's compiled SQL,
    which embeds their policy values, is never served to another. Only successes are kept.
    """

    def __init__(self, max_entries: int = 256) -> None:
        """Create an empty LRU cache holding at most ``max_entries`` compiled queries."""
        self._max = max_entries
        self._items: OrderedDict[str, CompiledQuery] = OrderedDict()
        self._lock = Lock()

    @staticmethod
    def key(
        logical_sql: str, mdl: Mdl, principal: Principal, policies: Sequence[RowPolicy]
    ) -> str:
        """Hash of every compiler input, including the principal, used as the cache key."""
        payload = json.dumps(
            [
                logical_sql,
                mdl.model_dump(mode="json", by_alias=True),
                [policy.model_dump(mode="json", by_alias=True) for policy in policies],
                [principal.user_id, principal.role_name, dict(principal.attributes)],
            ],
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, key: str) -> CompiledQuery | None:
        """The cached query for ``key`` (marking it recently used), or None."""
        with self._lock:
            found = self._items.get(key)
            if found is not None:
                self._items.move_to_end(key)
            return found

    def put(self, key: str, compiled: CompiledQuery) -> None:
        """Store a compiled query, evicting the least recently used entry when full."""
        with self._lock:
            self._items[key] = compiled
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    def clear(self) -> None:
        """Drop every cached query."""
        with self._lock:
            self._items.clear()


compile_cache = CompileCache()


def compile_for(
    logical_sql: str, mdl: Mdl, principal: Principal, policies: Sequence[RowPolicy]
) -> CompiledQuery | tuple[CompileError, ...]:
    """The compiled query, or the errors; an unexpected compiler error fails closed."""
    key = CompileCache.key(logical_sql, mdl, principal, policies)
    cached = compile_cache.get(key)
    if cached is not None:
        return cached
    try:
        compiled = compile_query(logical_sql, mdl, principal, policies)
    except Exception:  # noqa: BLE001 - fail closed, typed
        return (CompileError(CompileErrorCode.SYNTAX_ERROR, "the query could not be compiled"),)
    if not compiled.ok:
        return tuple(compiled.errors)
    compile_cache.put(key, compiled)
    return compiled


def execute_compiled(
    compiled: CompiledQuery, mdl: Mdl, warehouse: WarehousePort, max_rows: int
) -> LogicalRun:
    """Re-validate the compiled SQL, then explain and run it read-only; failures stay typed."""
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
    """Compile logical SQL for ``principal`` and execute it, returning a ``LogicalRun``."""
    compiled = compile_for(logical_sql, mdl, principal, policies)
    if isinstance(compiled, tuple):
        return LogicalRun(failure_from(compiled), compilation_errors=compiled)
    return execute_compiled(compiled, mdl, warehouse, max_rows)

