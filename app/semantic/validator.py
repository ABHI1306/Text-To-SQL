"""Validator for compiled SQL: a pure allowlist check that runs before anything executes.

The compiler is the only producer of executable SQL, so this checks that the text really has
the shape the compiler emits: one SELECT, no writes or blocked functions, and physical tables
only as the single source of a ``m_<model>`` CTE whose table is the model's own reference.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp, parse
from sqlglot.errors import SqlglotError

from app.domain.mdl import Mdl
from app.semantic.compiler import (
    DIALECT,
    MODEL_CTE_PREFIX,
    CompileError,
    CompileErrorCode,
    cte_reference_ids,
    find_forbidden_construct,
)


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of validating compiled SQL: the errors found, if any."""
    errors: tuple[CompileError, ...]

    @property
    def ok(self) -> bool:
        """True when no errors were found."""
        return not self.errors


def _fail(code: CompileErrorCode, message: str, identifier: str | None = None) -> ValidationResult:
    """A failed result holding one error."""
    return ValidationResult((CompileError(code, message, identifier),))


def validate_compiled_sql(sql: str, mdl: Mdl) -> ValidationResult:
    """Parse and allowlist-check ``sql`` (psycopg pyformat text) against the deployed MDL."""
    try:
        # Query text is psycopg pyformat: a literal % is written %%.
        statements = [s for s in parse(sql.replace("%%", "%"), read=DIALECT) if s is not None]
    except (SqlglotError, RecursionError):
        return _fail(CompileErrorCode.SYNTAX_ERROR, "compiled SQL does not parse")
    if len(statements) != 1:
        return _fail(CompileErrorCode.NON_SELECT, "exactly one statement is allowed")
    root = statements[0]
    if not isinstance(root, (exp.Select, exp.SetOperation)):
        return _fail(CompileErrorCode.NON_SELECT, "only a SELECT statement is allowed")
    try:
        problem = find_forbidden_construct(root, allow_parameters=True)
        if problem is not None:
            return ValidationResult((problem,))
        return _check_relations(root, mdl)
    except RecursionError:
        return _fail(CompileErrorCode.SYNTAX_ERROR, "compiled SQL is nested too deeply")


def _check_relations(root: exp.Expr, mdl: Mdl) -> ValidationResult:
    """Require each model CTE to select only from its model's table and no other relations."""
    tables = {model.name: model.table_reference.lower() for model in mdl.models}
    cte_refs = cte_reference_ids(root)
    model_ctes: dict[str, exp.CTE] = {}
    for cte in root.find_all(exp.CTE):
        name = cte.alias.lower()
        if name.startswith(MODEL_CTE_PREFIX) and name[len(MODEL_CTE_PREFIX) :] in tables:
            model_ctes[name] = cte
    errors: list[CompileError] = []
    allowed_physical: set[int] = set()
    for name, cte in model_ctes.items():
        body_tables = list(cte.this.find_all(exp.Table))
        expected = tables[name[len(MODEL_CTE_PREFIX) :]]
        if len(body_tables) != 1 or _qualified(body_tables[0]) != expected:
            errors.append(
                CompileError(
                    CompileErrorCode.UNKNOWN_MODEL,
                    f"{name} must select only from {expected}",
                    identifier=name,
                )
            )
        else:
            allowed_physical.add(id(body_tables[0]))
    for table in root.find_all(exp.Table):
        if id(table) in allowed_physical:
            continue
        if not table.db and not table.catalog and id(table) in cte_refs:
            continue
        shown = _qualified(table)
        errors.append(
            CompileError(
                CompileErrorCode.UNKNOWN_MODEL,
                f"relation {shown!r} was not emitted by the compiler",
                identifier=shown,
            )
        )
    return ValidationResult(tuple(errors))


def _qualified(table: exp.Table) -> str:
    """Lowercase dotted name of a table, including catalog and schema."""
    return ".".join(part for part in (table.catalog, table.db, table.name) if part).lower()
