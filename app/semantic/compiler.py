"""Semantic compiler: logical SQL over MDL models -> the only SQL allowed to reach the database.

A pure function of the logical SQL, the deployed MDL, the principal and the row policies.
Parameters use the psycopg pyformat style (``%(p_region)s``); literal ``%`` characters in the
emitted SQL are doubled so the output is always executed with a parameter mapping.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from sqlglot import exp, parse
from sqlglot.errors import OptimizeError, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from app.core.types import JsonValue, Principal
from app.domain.mdl import Mdl, MdlColumn, MdlModel, Metric, Relationship
from app.domain.policy import ATTRIBUTE_PLACEHOLDER, RowPolicy

DIALECT = "postgres"
DEFAULT_LIMIT = 1000
HARD_LIMIT = 10_001  # the largest row cap the API allows (10,000) plus the truncation probe
MODEL_CTE_PREFIX = "m_"
METRIC_CTE_PREFIX = "metric_"
MAX_CALCULATION_DEPTH = 10
MAX_SQL_CHARS = 20_000

DISALLOWED_FUNCTION_PREFIXES = ("pg_", "lo_", "dblink", "txid_", "xmlbinary")
DISALLOWED_FUNCTIONS = frozenset(
    {
        "setconfig",
        "currentsetting",
        "version",
        "currentuser",
        "sessionuser",
        "currentdatabase",
        "currentschema",
        "currentschemas",
        "currentversion",
        "inetserveraddr",
        "inetclientaddr",
        "querytoxml",
        "tabletoxml",
        "databasetoxml",
        "schematoxml",
        "tsstat",
        "hastableprivilege",
        "hasdatabaseprivilege",
        "hasschemaprivilege",
        "hascolumnprivilege",
    }
)


class CompileErrorCode(StrEnum):
    """Stable machine-readable codes for every way compilation or validation fails."""
    NON_SELECT = "NON_SELECT"
    SYNTAX_ERROR = "SYNTAX_ERROR"
    UNKNOWN_MODEL = "UNKNOWN_MODEL"
    UNKNOWN_COLUMN = "UNKNOWN_COLUMN"
    DISALLOWED_FUNCTION = "DISALLOWED_FUNCTION"
    JOIN_NOT_ALLOWED = "JOIN_NOT_ALLOWED"
    INVALID_MODEL = "INVALID_MODEL"
    POLICY_ERROR = "POLICY_ERROR"
    DRY_RUN_FAILED = "DRY_RUN_FAILED"


@dataclass(frozen=True)
class CompileError:
    """One compile or validation failure, with the offending name and close-match hints."""
    code: CompileErrorCode
    message: str
    identifier: str | None = None
    suggestions: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompiledQuery:
    """Compiler output: executable SQL and its bind parameters, or the errors."""
    sql: str
    parameters: Mapping[str, JsonValue] = field(default_factory=dict)
    policy_context: str = ""
    physical_relations: tuple[str, ...] = ()
    applied_policies: tuple[str, ...] = ()
    missing_attributes: tuple[str, ...] = ()
    errors: tuple[CompileError, ...] = ()

    @property
    def ok(self) -> bool:
        """True when compilation produced no errors."""
        return not self.errors


class _Failure(Exception):
    """Internal control-flow exception carrying the ``CompileError`` list to report."""
    def __init__(self, *errors: CompileError) -> None:
        """Keep the errors and use the first message as the exception text."""
        super().__init__(errors[0].message)
        self.errors = errors


def compile_query(
    logical_sql: str,
    mdl: Mdl,
    principal: Principal | None,
    policies: Sequence[RowPolicy] = (),
    *,
    default_limit: int = DEFAULT_LIMIT,
) -> CompiledQuery:
    """Compile logical SQL. ``principal=None`` applies no row policies (structural checks only)."""
    if len(logical_sql) > MAX_SQL_CHARS:
        return CompiledQuery(
            sql="",
            errors=(CompileError(CompileErrorCode.SYNTAX_ERROR, "the query is too long"),),
        )
    try:
        return _Compilation(mdl, principal, policies, default_limit).run(logical_sql)
    except _Failure as failure:
        return CompiledQuery(sql="", errors=failure.errors)
    except RecursionError:
        return CompiledQuery(
            sql="",
            errors=(CompileError(CompileErrorCode.SYNTAX_ERROR, "the query is nested too deeply"),),
        )


class _Compilation:
    """One compilation run: holds the MDL, principal and policies and rewrites a query."""
    def __init__(
        self,
        mdl: Mdl,
        principal: Principal | None,
        policies: Sequence[RowPolicy],
        default_limit: int,
    ) -> None:
        """Index models and metrics by name and set up per-run bind state."""
        self.mdl = mdl
        self.principal = principal
        self.policies = policies
        self.default_limit = default_limit
        self.models = {model.name: model for model in mdl.models}
        self.metrics = {metric.name: metric for metric in mdl.metrics}
        self.relationships = mdl.relationships
        self.nonce = uuid.uuid4().hex[:12]
        self.parameters: dict[str, JsonValue] = {}
        self.tokens: dict[str, str] = {}
        self.missing: list[str] = []
        self.applied: list[tuple[RowPolicy, dict[str, JsonValue | None]]] = []

    # ------------------------------------------------------------------ pipeline

    def run(self, logical_sql: str) -> CompiledQuery:
        """Run the pipeline: restrict, resolve, expand, check joins, wrap in CTEs, emit."""
        root = self._parse_and_restrict(logical_sql)
        cte_names = self._collect_cte_names(root)
        self._resolve_relations(root, cte_names)
        self._expand_metrics(root, cte_names)
        self._check_joins(root)
        used_columns = self._used_columns(root, cte_names)
        used_models = self._rewrite_model_references(root, cte_names)
        model_ctes = [
            self._model_cte(self.models[name], used_columns.get(name, set()))
            for name in used_models
        ]
        self._prepend_ctes(root, model_ctes)
        if not root.args.get("limit") and isinstance(root, exp.Query):
            root.limit(self.default_limit, copy=False)
        else:
            self._clamp_limit(root)
        sql = self._emit(root)
        return CompiledQuery(
            sql=sql,
            parameters=dict(self.parameters),
            policy_context=self._policy_context(),
            physical_relations=tuple(self.models[name].table_reference for name in used_models),
            applied_policies=tuple(policy.name for policy, _ in self.applied),
            missing_attributes=tuple(dict.fromkeys(self.missing)),
        )

    def _clamp_limit(self, root: exp.Expr) -> None:
        """A written LIMIT is kept up to the hard ceiling, so one query cannot ask for billions."""
        limit = root.args.get("limit")
        if not isinstance(limit, exp.Limit):
            return
        count = limit.args.get("expression")
        if (
            isinstance(count, exp.Literal)
            and not count.is_string
            and count.this.isdigit()
            and int(count.this) > HARD_LIMIT
        ):
            limit.set("expression", exp.Literal.number(HARD_LIMIT))

    # -------------------------------------------------------------- 1. parse and restrict

    def _parse_and_restrict(self, logical_sql: str) -> exp.Expr:
        """Parse exactly one SELECT and reject writes, DDL and forbidden nodes."""
        try:
            statements = [s for s in parse(logical_sql, read=DIALECT) if s is not None]
        except SqlglotError as error:
            raise _Failure(
                CompileError(CompileErrorCode.SYNTAX_ERROR, _syntax_message(error))
            ) from error
        if not statements:
            raise _Failure(CompileError(CompileErrorCode.SYNTAX_ERROR, "empty SQL"))
        if len(statements) > 1:
            raise _Failure(
                CompileError(CompileErrorCode.NON_SELECT, "exactly one SELECT is allowed")
            )
        root = statements[0]
        while isinstance(root, exp.Subquery) and not root.args.get("alias"):
            root = root.this
        if not isinstance(root, (exp.Select, exp.SetOperation)):
            raise _Failure(
                CompileError(CompileErrorCode.NON_SELECT, "only SELECT statements are allowed")
            )
        if isinstance(root, exp.Select) and not root.expressions:
            raise _Failure(CompileError(CompileErrorCode.SYNTAX_ERROR, "SELECT needs a column list"))
        self._reject_forbidden_nodes(root)
        return root

    def _reject_forbidden_nodes(self, root: exp.Expr) -> None:
        """Raise if the tree has a write, parameter, table function or blocked function."""
        problem = find_forbidden_construct(root, allow_parameters=False)
        if problem is not None:
            raise _Failure(problem)

    # -------------------------------------------------------------- 2. resolve relations

    def _collect_cte_names(self, root: exp.Expr) -> set[str]:
        """Collect user WITH names, rejecting any that shadow a model, metric or generated CTE."""
        names: set[str] = set()
        for cte in root.find_all(exp.CTE):
            name = cte.alias.lower()
            reserved = name in self.models or name in self.metrics
            generated = name.startswith((MODEL_CTE_PREFIX, METRIC_CTE_PREFIX)) and (
                name[len(MODEL_CTE_PREFIX) :] in self.models
                or name[len(METRIC_CTE_PREFIX) :] in self.metrics
            )
            if reserved or generated:
                raise _Failure(
                    CompileError(
                        CompileErrorCode.UNKNOWN_MODEL,
                        f"WITH name {name!r} clashes with a model or metric",
                        identifier=name,
                    )
                )
            names.add(name)
        return names

    def _resolve_relations(self, root: exp.Expr, cte_names: set[str]) -> None:
        """Reject any table that is schema-qualified or not a model, metric or WITH name."""
        allowed = [*self.models, *self.metrics]
        errors: list[CompileError] = []
        cte_refs = cte_reference_ids(root)
        for table in root.find_all(exp.Table):
            name = _table_name(table)
            if not table.db and not table.catalog and id(table) in cte_refs:
                continue
            if table.db or table.catalog or name not in allowed:
                shown = ".".join(part for part in (table.catalog, table.db, table.name) if part)
                errors.append(
                    CompileError(
                        CompileErrorCode.UNKNOWN_MODEL,
                        f"{shown!r} is not a deployed model or metric",
                        identifier=shown,
                        suggestions=_closest(table.name, allowed),
                    )
                )
        if errors:
            raise _Failure(*_dedupe(errors))

    # ----------------------------------------------------------------- 3. metrics

    def _expand_metrics(self, root: exp.Expr, cte_names: set[str]) -> None:
        """Replace metric references with grouped CTEs over the base model.

        The CTE bodies are logical SQL over models, so the model wrapping, column analysis and
        row policies below apply to them like any other query text.
        """
        used: dict[str, Metric] = {}
        cte_refs = cte_reference_ids(root)
        for table in list(root.find_all(exp.Table)):
            name = _table_name(table)
            if table.db or id(table) in cte_refs or name not in self.metrics:
                continue
            used[name] = self.metrics[name]
            table.replace(
                exp.Table(
                    this=exp.to_identifier(METRIC_CTE_PREFIX + name),
                    alias=exp.TableAlias(this=exp.to_identifier(table.alias or table.name)),
                )
            )
        cte_names.update(METRIC_CTE_PREFIX + name for name in used)
        self._prepend_ctes(root, [self._metric_cte(metric) for metric in used.values()])

    def _metric_cte(self, metric: Metric) -> exp.CTE:
        """Build the grouped CTE for ``metric`` over its base model, in logical SQL."""
        if metric.base_object not in self.models:
            raise _Failure(
                CompileError(
                    CompileErrorCode.INVALID_MODEL,
                    f"metric {metric.name!r} has unknown base model {metric.base_object!r}",
                    identifier=metric.name,
                )
            )
        projections = [
            exp.alias_(_parse_expression(field.expression), field.name)
            for field in metric.dimensions + metric.measures
        ]
        body = exp.select(*projections).from_(exp.to_table(metric.base_object))
        if metric.measures and metric.dimensions:
            body = body.group_by(*[exp.Literal.number(i + 1) for i in range(len(metric.dimensions))])
        elif not metric.measures:
            body = body.distinct()
        return exp.CTE(
            this=body,
            alias=exp.TableAlias(this=exp.to_identifier(METRIC_CTE_PREFIX + metric.name)),
        )

    # ------------------------------------------------------------- 4. column analysis

    def _schema(self) -> dict[str, object]:
        """Model-to-column-type mapping used to qualify column names with sqlglot."""
        return {
            model.name: {column.name: column.type for column in model.columns}
            for model in self.models.values()
        }

    def _used_columns(self, root: exp.Expr, cte_names: set[str]) -> dict[str, set[str]]:
        """Columns of each model the query uses, by resolving names on a qualified copy."""
        copy = root.copy()
        try:
            qualified = qualify(
                copy,
                schema=self._schema(),
                dialect=DIALECT,
                quote_identifiers=False,
                identify=False,
                validate_qualify_columns=True,
            )
        except OptimizeError as error:
            raise self._column_failure(root, error) from error
        except SqlglotError as error:
            raise _Failure(
                CompileError(CompileErrorCode.SYNTAX_ERROR, f"SQL could not be analysed: {error}")
            ) from error
        used: dict[str, set[str]] = {}
        for scope in traverse_scope(qualified):
            for column in [*scope.columns, *scope.external_columns]:
                source = _lookup_source(scope, column.table)
                if (
                    isinstance(source, exp.Table)
                    and not source.db
                    and _table_name(source) in self.models
                ):
                    used.setdefault(_table_name(source), set()).add(column.name.lower())
        return used

    def _column_failure(self, root: exp.Expr, error: OptimizeError) -> _Failure:
        """Turn a sqlglot qualification error into an ``UNKNOWN_COLUMN`` failure with hints."""
        message = str(error)
        identifier = message.split("'")[1] if message.count("'") >= 2 else None
        candidates = sorted(
            {
                column.name
                for table in root.find_all(exp.Table)
                if _table_name(table) in self.models
                for column in self.models[_table_name(table)].columns
            }
        )
        return _Failure(
            CompileError(
                CompileErrorCode.UNKNOWN_COLUMN,
                message.split(". Line")[0],
                identifier=identifier,
                suggestions=_closest(identifier or "", candidates),
            )
        )

    # -------------------------------------------------------------------- 5. joins

    def _check_joins(self, root: exp.Expr) -> None:
        """Check every join in every scope against the declared relationships."""
        cte_refs = cte_reference_ids(root)
        for scope in traverse_scope(root):
            select = scope.expression
            if not isinstance(select, exp.Select) or not select.args.get("joins"):
                continue
            first = select.args.get("from_")
            sources: list[exp.Expr] = [first.this] if first is not None else []
            for join in select.args["joins"]:
                self._check_join(scope, join, list(sources), cte_refs)
                sources.append(join.this)

    def _check_join(
        self, scope: Scope, join: exp.Join, left: list[exp.Expr], cte_refs: set[int]
    ) -> None:
        """Allow a model join only if it matches a declared relationship; fill in a missing ON."""
        right = join.this
        right_model = self._model_of(right, cte_refs)
        if right_model is None:
            return
        left_models = [
            (source.alias_or_name, model)
            for source in left
            if (model := self._model_of(source, cte_refs)) is not None
        ]
        if not left_models:
            return
        matches = [
            (alias, relationship)
            for alias, model in left_models
            if (relationship := self._relationship(model, right_model)) is not None
        ]
        description = f"{', '.join(model for _, model in left_models)} JOIN {right_model}"
        if not matches:
            raise _Failure(
                CompileError(
                    CompileErrorCode.JOIN_NOT_ALLOWED,
                    f"no declared relationship between {description.replace(' JOIN ', ' and ')}",
                    identifier=description,
                    suggestions=tuple(
                        sorted(self._related_models(right_model) | self._related_models(left_models[-1][1]))
                    ),
                )
            )
        condition = join.args.get("on")
        if condition is None and not join.args.get("using"):
            if str(join.args.get("kind") or "").upper() == "CROSS":
                raise _Failure(
                    CompileError(
                        CompileErrorCode.JOIN_NOT_ALLOWED,
                        "CROSS JOIN between models is not allowed",
                        identifier=description,
                    )
                )
            left_alias, relationship = matches[-1]
            join.set(
                "on",
                _relationship_condition(
                    relationship,
                    {
                        _model_for_alias(left_models, left_alias): left_alias,
                        right_model: right.alias_or_name,
                    },
                ),
            )
            return
        provided = self._join_pairs(scope, join, left_models, right_model, cte_refs)
        for _alias, relationship in matches:
            required = _condition_pairs(relationship.condition)
            if required <= provided:
                return
        raise _Failure(
            CompileError(
                CompileErrorCode.JOIN_NOT_ALLOWED,
                f"join condition does not match the declared relationship for {description}",
                identifier=description,
                suggestions=tuple(relationship.condition for _, relationship in matches),
            )
        )

    def _join_pairs(
        self,
        scope: Scope,
        join: exp.Join,
        left_models: list[tuple[str, str]],
        right_model: str,
        cte_refs: set[int],
    ) -> set[frozenset[tuple[str, str]]]:
        """Column pairs equated by the join's ON or USING, as (model, column) sets."""
        alias_models = dict(left_models)
        alias_models[join.this.alias_or_name] = right_model
        pairs: set[frozenset[tuple[str, str]]] = set()
        for using in join.args.get("using") or []:
            for _, model in left_models:
                pairs.add(frozenset({(model, using.name), (right_model, using.name)}))
        condition = join.args.get("on")
        if condition is None:
            return pairs
        for equality in _conjuncts(condition):
            if not isinstance(equality, exp.EQ):
                continue
            sides = [self._resolve_pair_side(side, alias_models) for side in
                     (equality.this, equality.expression)]
            if all(side is not None for side in sides):
                pairs.add(frozenset(side for side in sides if side is not None))
        return pairs

    def _resolve_pair_side(
        self, side: exp.Expr, alias_models: dict[str, str]
    ) -> tuple[str, str] | None:
        """Resolve one side of an equality to (model, column), or None if ambiguous."""
        if not isinstance(side, exp.Column):
            return None
        if side.table:
            model = alias_models.get(side.table)
            return (model, side.name.lower()) if model else None
        owners = [
            model
            for model in alias_models.values()
            if side.name.lower() in {c.name for c in self.models[model].columns}
        ]
        return (owners[0], side.name.lower()) if len(owners) == 1 else None

    def _model_of(self, source: exp.Expr, cte_refs: set[int]) -> str | None:
        """Model name if ``source`` is a direct model table (not a CTE or qualified), else None."""
        if isinstance(source, exp.Table) and not source.db and id(source) not in cte_refs:
            name = _table_name(source)
            if name in self.models:
                return name
        return None

    def _relationship(self, left: str, right: str) -> Relationship | None:
        """The declared relationship between two models, if any."""
        for relationship in self.relationships:
            if set(relationship.models) == {left, right}:
                return relationship
        return None

    def _related_models(self, model: str) -> set[str]:
        """Models that share a declared relationship with ``model``."""
        related: set[str] = set()
        for relationship in self.relationships:
            if model in relationship.models:
                related |= set(relationship.models) - {model}
        return related

    # --------------------------------------------------- 6. model references and CTEs

    def _rewrite_model_references(self, root: exp.Expr, cte_names: set[str]) -> list[str]:
        """Point each model table at its ``m_<model>`` CTE and return the models used."""
        used: dict[str, None] = {}
        cte_refs = cte_reference_ids(root)
        for table in list(root.find_all(exp.Table)):
            name = _table_name(table)
            if table.db or id(table) in cte_refs or name not in self.models:
                continue
            used[name] = None
            alias = table.alias or table.name
            table.replace(
                exp.Table(
                    this=exp.to_identifier(MODEL_CTE_PREFIX + name),
                    alias=exp.TableAlias(this=exp.to_identifier(alias)),
                )
            )
        return list(used)

    def _model_cte(self, model: MdlModel, used: set[str]) -> exp.CTE:
        """Build the ``m_<model>`` CTE: used columns plus the row-policy filters."""
        columns = [column for column in model.columns if column.name in used]
        if not columns:
            columns = [next((c for c in model.columns if not c.is_calculated), model.columns[0])]
        projections = [self._projection(model, column) for column in columns]
        select = exp.select(*projections).from_(
            exp.to_table(model.table_reference, dialect=DIALECT)
        )
        for predicate in self._policy_predicates(model):
            select = select.where(predicate, copy=False)
        return exp.CTE(
            this=select,
            alias=exp.TableAlias(this=exp.to_identifier(MODEL_CTE_PREFIX + model.name)),
        )

    def _projection(self, model: MdlModel, column: MdlColumn) -> exp.Expr:
        """Project a column (physical or calculated) under its logical name."""
        if column.is_calculated:
            expression = self._physical_expression(model, column.expression or "", 0)
            return exp.alias_(expression, column.name)
        physical = column.physical_name or column.name
        reference = exp.column(physical)
        return reference if physical == column.name else exp.alias_(reference, column.name)

    def _physical_expression(self, model: MdlModel, expression: str, depth: int) -> exp.Expr:
        """Rewrite a model-level expression over logical column names to physical columns."""
        if depth > MAX_CALCULATION_DEPTH:
            raise _Failure(
                CompileError(
                    CompileErrorCode.INVALID_MODEL,
                    f"calculated fields of model {model.name!r} are circular",
                    identifier=model.name,
                )
            )
        by_name = {column.name: column for column in model.columns}
        parsed = _parse_expression(expression)
        for node in list(parsed.find_all(exp.Column)):
            column = by_name.get(node.name.lower())
            if column is None:
                raise _Failure(
                    CompileError(
                        CompileErrorCode.UNKNOWN_COLUMN,
                        f"unknown column {node.name!r} in model {model.name!r}",
                        identifier=node.name,
                        suggestions=_closest(node.name, list(by_name)),
                    )
                )
            if column.is_calculated:
                replacement: exp.Expr = exp.Paren(
                    this=self._physical_expression(model, column.expression or "", depth + 1)
                )
            else:
                replacement = exp.column(column.physical_name or column.name)
            if node is parsed:
                parsed = replacement
            else:
                node.replace(replacement)
        return parsed

    def _prepend_ctes(self, root: exp.Expr, ctes: list[exp.CTE]) -> None:
        """Put ``ctes`` ahead of any existing WITH entries of ``root``."""
        if not ctes:
            return
        existing = root.args.get("with_")
        if existing is None:
            root.set("with_", exp.With(expressions=ctes))
        else:
            existing.set("expressions", [*ctes, *existing.expressions])

    # --------------------------------------------------------------- 7. row policies

    def _policy_predicates(self, model: MdlModel) -> list[exp.Expr]:
        """Predicates of the enabled policies that apply to ``model`` for the principal."""
        if self.principal is None:
            return []
        predicates: list[exp.Expr] = []
        for policy in self.policies:
            if policy.model != model.name or not policy.applies_to_role(self.principal.role_name):
                continue
            predicate, values = self._render_policy(model, policy, self.principal)
            predicates.append(predicate)
            self.applied.append((policy, values))
        return predicates

    def _render_policy(
        self, model: MdlModel, policy: RowPolicy, principal: Principal
    ) -> tuple[exp.Expr, dict[str, JsonValue | None]]:
        """Render a policy predicate with its attributes bound as parameters.

        A missing attribute yields ``FALSE`` (deny all rows) and is recorded; list attributes are
        only allowed with ``=`` or ``IN`` and become ``= ANY(param)``.
        """
        values: dict[str, JsonValue | None] = {
            attribute: principal.attributes.get(attribute) for attribute in policy.attributes()
        }
        missing = [name for name, value in values.items() if value is None]
        if missing:
            self.missing.extend(missing)
            return exp.false(), values
        predicate = self._parse_predicate(model, policy)
        sentinels = {_sentinel(name): name for name in values}
        for node in list(predicate.find_all(exp.Column)):
            attribute = sentinels.get(node.name)
            if attribute is None or node.table:
                continue
            value = values[attribute]
            parameter = self._bind(attribute, value)
            parent = node.parent
            if isinstance(value, list):
                if isinstance(parent, exp.EQ) and parent.expression is node:
                    parent.replace(
                        exp.EQ(this=parent.this, expression=exp.Anonymous(this="ANY", expressions=[parameter]))
                    )
                    continue
                if isinstance(parent, exp.In) and parent.args.get("expressions") == [node]:
                    parent.replace(
                        exp.EQ(this=parent.this, expression=exp.Anonymous(this="ANY", expressions=[parameter]))
                    )
                    continue
                raise _Failure(
                    CompileError(
                        CompileErrorCode.POLICY_ERROR,
                        f"policy {policy.name!r}: list attribute {attribute!r} may only be used "
                        "with = or IN",
                        identifier=policy.name,
                    )
                )
            node.replace(parameter)
        return predicate, values

    def _parse_predicate(self, model: MdlModel, policy: RowPolicy) -> exp.Expr:
        """Parse and validate a policy predicate for ``model``, using physical column names."""
        text = ATTRIBUTE_PLACEHOLDER.sub(lambda match: _sentinel(match.group(1)), policy.predicate)
        if "{{" in text or "}}" in text:
            raise _Failure(
                CompileError(
                    CompileErrorCode.POLICY_ERROR,
                    f"policy {policy.name!r}: only {{{{ user.<attribute> }}}} placeholders are "
                    "supported",
                    identifier=policy.name,
                )
            )
        try:
            parsed = _parse_expression(text)
        except _Failure as failure:
            raise _Failure(
                CompileError(
                    CompileErrorCode.POLICY_ERROR,
                    f"policy {policy.name!r}: predicate does not parse",
                    identifier=policy.name,
                )
            ) from failure
        forbidden = find_forbidden_construct(parsed, allow_parameters=True)
        if forbidden is not None:
            raise _Failure(
                CompileError(
                    CompileErrorCode.POLICY_ERROR,
                    f"policy {policy.name!r}: {forbidden.message}",
                    identifier=policy.name,
                )
            )
        if any(isinstance(node, (exp.Query, exp.Subquery)) for node in parsed.walk()):
            raise _Failure(
                CompileError(
                    CompileErrorCode.POLICY_ERROR,
                    f"policy {policy.name!r}: subqueries are not allowed in predicates",
                    identifier=policy.name,
                )
            )
        sentinels = {_sentinel(name) for name in policy.attributes()}
        by_name = {column.name for column in model.columns}
        for node in list(parsed.find_all(exp.Column)):
            if node.name in sentinels and not node.table:
                continue
            if node.table and node.table.lower() != model.name or node.name.lower() not in by_name:
                raise _Failure(
                    CompileError(
                        CompileErrorCode.POLICY_ERROR,
                        f"policy {policy.name!r}: unknown column {node.sql(DIALECT)!r} "
                        f"for model {model.name!r}",
                        identifier=node.name,
                        suggestions=_closest(node.name, sorted(by_name)),
                    )
                )
        # Paren keeps a parent above the root so in-place replacements always take effect.
        return self._physicalise(model, exp.Paren(this=parsed), sentinels)

    def _physicalise(self, model: MdlModel, parsed: exp.Expr, keep: set[str]) -> exp.Expr:
        """Replace logical columns in ``parsed`` with physical ones, skipping ``keep`` sentinels."""
        by_name = {column.name: column for column in model.columns}
        for node in list(parsed.find_all(exp.Column)):
            if node.name in keep and not node.table:
                continue
            column = by_name[node.name.lower()]
            if column.is_calculated:
                replacement: exp.Expr = exp.Paren(
                    this=self._physical_expression(model, column.expression or "", 1)
                )
            else:
                replacement = exp.column(column.physical_name or column.name)
            if node is parsed:
                parsed = replacement
            else:
                node.replace(replacement)
        return parsed

    def _bind(self, attribute: str, value: JsonValue | None) -> exp.Expr:
        """Register a bind parameter for ``attribute`` and return a placeholder token column."""
        name = f"p_{attribute}"
        self.parameters[name] = value
        token = f"__p{self.nonce}_{attribute}__"
        self.tokens[token] = f"%({name})s"
        return exp.column(token)

    # ------------------------------------------------------------------- 8. emit

    def _emit(self, root: exp.Expr) -> str:
        """Render the SQL, escape literal percents and swap tokens for pyformat parameters."""
        sql = root.sql(DIALECT)
        sql = sql.replace("%", "%%")
        # Longest first: one attribute's token can be a prefix of another's (a vs a__b).
        for token in sorted(self.tokens, key=len, reverse=True):
            sql = sql.replace(token, self.tokens[token])
        return sql

    def _policy_context(self) -> str:
        """Hash of the applied policies and their values, identifying the row-security context."""
        payload = [
            {"name": policy.name, "predicate": policy.predicate, "values": values}
            for policy, values in sorted(self.applied, key=lambda item: item[0].name)
        ]
        encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


# ---------------------------------------------------------------------- helpers


def cte_reference_ids(root: exp.Expr) -> set[int]:
    """Table nodes that resolve to a WITH name under SQL scoping rules (by node identity).

    A CTE is visible only to later siblings and the statement that owns the WITH, never to its own
    body (unless RECURSIVE) or to an enclosing query, so a same-named physical relation elsewhere
    is not a CTE reference. Tables the scope walk never reaches are treated as physical.
    """
    references: set[int] = set()
    for scope in traverse_scope(root):
        for table in scope.tables:
            if isinstance(scope.sources.get(table.alias_or_name), Scope):
                references.add(id(table))
    return references


# Functions that sqlglot does not model as typed nodes must be listed here to be used.
ALLOWED_UNTYPED_FUNCTIONS = frozenset(
    {
        "any", "abs", "ceil", "ceiling", "floor", "round", "trunc", "mod", "power", "sqrt", "exp",
        "ln", "log", "sign", "width_bucket", "lower", "upper", "length", "char_length", "btrim",
        "ltrim", "rtrim", "trim", "lpad", "rpad", "substr", "substring", "replace", "concat",
        "concat_ws", "left", "right", "split_part", "strpos", "position", "initcap", "reverse",
        "translate", "date_trunc", "date_part", "extract", "now", "age", "to_char", "to_date",
        "to_timestamp", "make_date", "date_bin", "count", "sum", "avg", "min", "max", "array_agg",
        "string_agg", "bool_and", "bool_or", "stddev", "stddev_samp", "stddev_pop", "variance",
        "var_samp", "var_pop", "percentile_cont", "percentile_disc", "mode", "corr", "row_number",
        "rank", "dense_rank", "ntile", "lag", "lead", "first_value", "last_value", "nth_value",
        "cume_dist", "percent_rank", "coalesce", "nullif", "greatest", "least", "array_length",
        "cardinality", "date", "timezone",
    }
)
BLOCKED_CAST_TYPES = frozenset(
    {"regclass", "regproc", "regprocedure", "regoper", "regoperator", "regtype", "regnamespace",
     "regrole", "regconfig", "regdictionary", "oid"}
)


def find_forbidden_construct(root: exp.Expr, *, allow_parameters: bool) -> CompileError | None:
    """First write, DDL, parameter, table-function or blocked-function node in ``root``."""
    for node in root.walk():
        if isinstance(node, (exp.DML, exp.DDL, exp.Command, exp.Into, exp.Lock)):
            return CompileError(
                CompileErrorCode.NON_SELECT, "only read-only SELECT statements are allowed"
            )
        if not allow_parameters and isinstance(node, (exp.Placeholder, exp.Parameter)):
            return CompileError(CompileErrorCode.SYNTAX_ERROR, "query parameters are not allowed")
        if isinstance(node, exp.Table) and not isinstance(node.this, exp.Identifier):
            return CompileError(
                CompileErrorCode.DISALLOWED_FUNCTION,
                "table-valued functions are not allowed",
                identifier=node.this.sql(DIALECT) if node.this else None,
            )
        if isinstance(node, (exp.GenerateSeries, exp.ExplodingGenerateSeries)):
            return CompileError(
                CompileErrorCode.DISALLOWED_FUNCTION,
                "generate_series is not allowed",
                identifier="generate_series",
            )
        if isinstance(node, exp.DataType) and str(node.this).lower().split(".")[-1] in BLOCKED_CAST_TYPES:
            return CompileError(
                CompileErrorCode.DISALLOWED_FUNCTION,
                f"casts to {str(node.this).lower()} are not allowed",
                identifier=str(node.this).lower(),
            )
        if isinstance(node, (exp.Func, exp.CurrentUser, exp.CurrentSchema, exp.CurrentCatalog)):
            name = _function_name(node)
            untyped_unlisted = isinstance(node, exp.Anonymous) and name not in ALLOWED_UNTYPED_FUNCTIONS
            if (
                untyped_unlisted
                or name.replace("_", "") in DISALLOWED_FUNCTIONS
                or name.startswith(DISALLOWED_FUNCTION_PREFIXES)
                or isinstance(node, exp.CurrentCatalog)
            ):
                return CompileError(
                    CompileErrorCode.DISALLOWED_FUNCTION,
                    f"function {name!r} is not allowed",
                    identifier=name,
                )
    return None


def _syntax_message(error: SqlglotError) -> str:
    """User-facing text for a sqlglot parse error, with line and column when known."""
    details = getattr(error, "errors", None) or []
    if details and isinstance(details[0], dict):
        first = details[0]
        description = str(first.get("description", "invalid SQL")).strip()
        return f"SQL could not be parsed: {description} (line {first.get('line')}, column {first.get('col')})"
    return "SQL could not be parsed"


def _sentinel(attribute: str) -> str:
    """Placeholder column name standing in for a policy attribute during parsing."""
    return f"__attr_{attribute}__"


def _function_name(node: exp.Expr) -> str:
    """Lowercase function name of ``node`` (the call name for untyped functions)."""
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return str(node.key).lower()


def _table_name(table: exp.Table) -> str:
    """Table name, lowercased unless it was quoted."""
    identifier = table.this
    name = identifier.name if isinstance(identifier, exp.Identifier) else str(identifier)
    quoted = isinstance(identifier, exp.Identifier) and identifier.args.get("quoted")
    return name if quoted else name.lower()


def _parse_expression(sql: str) -> exp.Expr:
    """Parse ``sql`` as exactly one statement or expression, else raise a syntax failure."""
    try:
        statements = parse(sql, read=DIALECT)
    except SqlglotError as error:
        raise _Failure(CompileError(CompileErrorCode.SYNTAX_ERROR, str(error))) from error
    if len(statements) != 1 or statements[0] is None:
        raise _Failure(CompileError(CompileErrorCode.SYNTAX_ERROR, "expected one expression"))
    return statements[0]


def _closest(name: str, candidates: Sequence[str]) -> tuple[str, ...]:
    """Up to three close matches for ``name`` among ``candidates``."""
    return tuple(difflib.get_close_matches(name.lower(), list(candidates), n=3, cutoff=0.5))


def _dedupe(errors: list[CompileError]) -> list[CompileError]:
    """Drop errors that repeat the same code and identifier, keeping order."""
    seen: set[tuple[CompileErrorCode, str | None]] = set()
    unique: list[CompileError] = []
    for error in errors:
        key = (error.code, error.identifier)
        if key not in seen:
            seen.add(key)
            unique.append(error)
    return unique


def _lookup_source(scope: Scope | None, alias: str) -> object | None:
    """Find ``alias`` in ``scope`` or the nearest enclosing scope that defines it."""
    while scope is not None:
        if alias in scope.sources:
            return scope.sources[alias]
        scope = scope.parent
    return None


def _conjuncts(condition: exp.Expr) -> list[exp.Expr]:
    """Flatten nested AND and parentheses into a list of conditions."""
    if isinstance(condition, exp.And):
        return _conjuncts(condition.this) + _conjuncts(condition.expression)
    if isinstance(condition, exp.Paren):
        return _conjuncts(condition.this)
    return [condition]


def _condition_pairs(condition: str) -> set[frozenset[tuple[str, str]]]:
    """Column equalities of a relationship condition as (table, column) pair sets."""
    pairs: set[frozenset[tuple[str, str]]] = set()
    for equality in _conjuncts(_parse_expression(condition)):
        if not isinstance(equality, exp.EQ):
            continue
        sides = [side for side in (equality.this, equality.expression) if isinstance(side, exp.Column)]
        if len(sides) == 2:
            pairs.add(frozenset((side.table.lower(), side.name.lower()) for side in sides))
    return pairs


def _model_for_alias(left_models: list[tuple[str, str]], alias: str) -> str:
    """Model name behind ``alias`` in the left-hand join sources."""
    return next(model for name, model in left_models if name == alias)


def _relationship_condition(relationship: Relationship, aliases: dict[str, str]) -> exp.Expr:
    """Relationship condition with its model names replaced by the query's aliases."""
    condition = _parse_expression(relationship.condition)
    for column in list(condition.find_all(exp.Column)):
        alias = aliases.get(column.table.lower())
        if alias is not None:
            column.set("table", exp.to_identifier(alias))
    return condition
