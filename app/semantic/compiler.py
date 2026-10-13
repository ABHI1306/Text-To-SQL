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
MODEL_CTE_PREFIX = "m_"
METRIC_CTE_PREFIX = "metric_"
MAX_CALCULATION_DEPTH = 10

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
    NON_SELECT = "NON_SELECT"
    SYNTAX_ERROR = "SYNTAX_ERROR"
    UNKNOWN_MODEL = "UNKNOWN_MODEL"
    UNKNOWN_COLUMN = "UNKNOWN_COLUMN"
    DISALLOWED_FUNCTION = "DISALLOWED_FUNCTION"
    JOIN_NOT_ALLOWED = "JOIN_NOT_ALLOWED"
    INVALID_MODEL = "INVALID_MODEL"
    POLICY_ERROR = "POLICY_ERROR"


@dataclass(frozen=True)
class CompileError:
    code: CompileErrorCode
    message: str
    identifier: str | None = None
    suggestions: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    parameters: Mapping[str, JsonValue] = field(default_factory=dict)
    policy_context: str = ""
    physical_relations: tuple[str, ...] = ()
    applied_policies: tuple[str, ...] = ()
    missing_attributes: tuple[str, ...] = ()
    errors: tuple[CompileError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


class _Failure(Exception):
    def __init__(self, *errors: CompileError) -> None:
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
    try:
        return _Compilation(mdl, principal, policies, default_limit).run(logical_sql)
    except _Failure as failure:
        return CompiledQuery(sql="", errors=failure.errors)


class _Compilation:
    def __init__(
        self,
        mdl: Mdl,
        principal: Principal | None,
        policies: Sequence[RowPolicy],
        default_limit: int,
    ) -> None:
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
        root = self._parse_and_restrict(logical_sql)
        cte_names = self._collect_cte_names(root)
        self._resolve_relations(root, cte_names)
        self._expand_metrics(root, cte_names)
        self._check_joins(root, cte_names)
        used_columns = self._used_columns(root, cte_names)
        used_models = self._rewrite_model_references(root, cte_names)
        model_ctes = [
            self._model_cte(self.models[name], used_columns.get(name, set()))
            for name in used_models
        ]
        self._prepend_ctes(root, model_ctes)
        if not root.args.get("limit") and isinstance(root, exp.Query):
            root.limit(self.default_limit, copy=False)
        sql = self._emit(root)
        return CompiledQuery(
            sql=sql,
            parameters=dict(self.parameters),
            policy_context=self._policy_context(),
            physical_relations=tuple(self.models[name].table_reference for name in used_models),
            applied_policies=tuple(policy.name for policy, _ in self.applied),
            missing_attributes=tuple(dict.fromkeys(self.missing)),
        )

    # -------------------------------------------------------------- 1. parse and restrict

    def _parse_and_restrict(self, logical_sql: str) -> exp.Expr:
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
        self._reject_forbidden_nodes(root)
        return root

    def _reject_forbidden_nodes(self, root: exp.Expr) -> None:
        for node in root.walk():
            if isinstance(node, (exp.DML, exp.DDL, exp.Command, exp.Into, exp.Lock)):
                raise _Failure(
                    CompileError(
                        CompileErrorCode.NON_SELECT, "only read-only SELECT statements are allowed"
                    )
                )
            if isinstance(node, (exp.Placeholder, exp.Parameter)):
                raise _Failure(
                    CompileError(
                        CompileErrorCode.SYNTAX_ERROR, "query parameters are not allowed"
                    )
                )
            if isinstance(node, exp.Table) and not isinstance(node.this, exp.Identifier):
                raise _Failure(
                    CompileError(
                        CompileErrorCode.DISALLOWED_FUNCTION,
                        "table-valued functions are not allowed",
                        identifier=node.this.sql(DIALECT) if node.this else None,
                    )
                )
            if isinstance(node, (exp.Func, exp.CurrentUser, exp.CurrentSchema)):
                name = _function_name(node)
                if name.replace("_", "") in DISALLOWED_FUNCTIONS or name.startswith(
                    DISALLOWED_FUNCTION_PREFIXES
                ):
                    raise _Failure(
                        CompileError(
                            CompileErrorCode.DISALLOWED_FUNCTION,
                            f"function {name!r} is not allowed",
                            identifier=name,
                        )
                    )

    # -------------------------------------------------------------- 2. resolve relations

    def _collect_cte_names(self, root: exp.Expr) -> set[str]:
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
        allowed = [*self.models, *self.metrics]
        errors: list[CompileError] = []
        for table in root.find_all(exp.Table):
            name = _table_name(table)
            if not table.db and not table.catalog and name in cte_names:
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
        for table in list(root.find_all(exp.Table)):
            name = _table_name(table)
            if table.db or name in cte_names or name not in self.metrics:
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

    def _check_joins(self, root: exp.Expr, cte_names: set[str]) -> None:
        for scope in traverse_scope(root):
            select = scope.expression
            if not isinstance(select, exp.Select) or not select.args.get("joins"):
                continue
            first = select.args.get("from_")
            sources: list[exp.Expr] = [first.this] if first is not None else []
            for join in select.args["joins"]:
                self._check_join(scope, join, list(sources), cte_names)
                sources.append(join.this)

    def _check_join(
        self, scope: Scope, join: exp.Join, left: list[exp.Expr], cte_names: set[str]
    ) -> None:
        right = join.this
        right_model = self._model_of(right, cte_names)
        if right_model is None:
            return
        left_models = [
            (source.alias_or_name, model)
            for source in left
            if (model := self._model_of(source, cte_names)) is not None
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
        provided = self._join_pairs(scope, join, left_models, right_model, cte_names)
        for alias, relationship in matches:
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
        cte_names: set[str],
    ) -> set[frozenset[tuple[str, str]]]:
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

    def _model_of(self, source: exp.Expr, cte_names: set[str]) -> str | None:
        if isinstance(source, exp.Table) and not source.db:
            name = _table_name(source)
            if name in self.models and name not in cte_names:
                return name
        return None

    def _relationship(self, left: str, right: str) -> Relationship | None:
        for relationship in self.relationships:
            if set(relationship.models) == {left, right}:
                return relationship
        return None

    def _related_models(self, model: str) -> set[str]:
        related: set[str] = set()
        for relationship in self.relationships:
            if model in relationship.models:
                related |= set(relationship.models) - {model}
        return related

    # --------------------------------------------------- 6. model references and CTEs

    def _rewrite_model_references(self, root: exp.Expr, cte_names: set[str]) -> list[str]:
        used: dict[str, None] = {}
        for table in list(root.find_all(exp.Table)):
            name = _table_name(table)
            if table.db or name in cte_names or name not in self.models:
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
        if not ctes:
            return
        existing = root.args.get("with_")
        if existing is None:
            root.set("with_", exp.With(expressions=ctes))
        else:
            existing.set("expressions", [*ctes, *existing.expressions])

    # --------------------------------------------------------------- 7. row policies

    def _policy_predicates(self, model: MdlModel) -> list[exp.Expr]:
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
        name = f"p_{attribute}"
        self.parameters[name] = value
        token = f"__p{self.nonce}_{attribute}__"
        self.tokens[token] = f"%({name})s"
        return exp.column(token)

    # ------------------------------------------------------------------- 8. emit

    def _emit(self, root: exp.Expr) -> str:
        sql = root.sql(DIALECT)
        sql = sql.replace("%", "%%")
        for token, placeholder in self.tokens.items():
            sql = sql.replace(token, placeholder)
        return sql

    def _policy_context(self) -> str:
        payload = [
            {"name": policy.name, "predicate": policy.predicate, "values": values}
            for policy, values in sorted(self.applied, key=lambda item: item[0].name)
        ]
        encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


# ---------------------------------------------------------------------- helpers


def _syntax_message(error: SqlglotError) -> str:
    details = getattr(error, "errors", None) or []
    if details and isinstance(details[0], dict):
        first = details[0]
        description = str(first.get("description", "invalid SQL")).strip()
        return f"SQL could not be parsed: {description} (line {first.get('line')}, column {first.get('col')})"
    return "SQL could not be parsed"


def _sentinel(attribute: str) -> str:
    return f"__attr_{attribute}__"


def _function_name(node: exp.Expr) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return str(node.key).lower()


def _table_name(table: exp.Table) -> str:
    identifier = table.this
    name = identifier.name if isinstance(identifier, exp.Identifier) else str(identifier)
    quoted = isinstance(identifier, exp.Identifier) and identifier.args.get("quoted")
    return name if quoted else name.lower()


def _parse_expression(sql: str) -> exp.Expr:
    try:
        statements = parse(sql, read=DIALECT)
    except SqlglotError as error:
        raise _Failure(CompileError(CompileErrorCode.SYNTAX_ERROR, str(error))) from error
    if len(statements) != 1 or statements[0] is None:
        raise _Failure(CompileError(CompileErrorCode.SYNTAX_ERROR, "expected one expression"))
    return statements[0]


def _closest(name: str, candidates: Sequence[str]) -> tuple[str, ...]:
    return tuple(difflib.get_close_matches(name.lower(), list(candidates), n=3, cutoff=0.5))


def _dedupe(errors: list[CompileError]) -> list[CompileError]:
    seen: set[tuple[CompileErrorCode, str | None]] = set()
    unique: list[CompileError] = []
    for error in errors:
        key = (error.code, error.identifier)
        if key not in seen:
            seen.add(key)
            unique.append(error)
    return unique


def _lookup_source(scope: Scope | None, alias: str) -> object | None:
    while scope is not None:
        if alias in scope.sources:
            return scope.sources[alias]
        scope = scope.parent
    return None


def _conjuncts(condition: exp.Expr) -> list[exp.Expr]:
    if isinstance(condition, exp.And):
        return _conjuncts(condition.this) + _conjuncts(condition.expression)
    if isinstance(condition, exp.Paren):
        return _conjuncts(condition.this)
    return [condition]


def _condition_pairs(condition: str) -> set[frozenset[tuple[str, str]]]:
    pairs: set[frozenset[tuple[str, str]]] = set()
    for equality in _conjuncts(_parse_expression(condition)):
        if not isinstance(equality, exp.EQ):
            continue
        sides = [side for side in (equality.this, equality.expression) if isinstance(side, exp.Column)]
        if len(sides) == 2:
            pairs.add(frozenset((side.table.lower(), side.name.lower()) for side in sides))
    return pairs


def _model_for_alias(left_models: list[tuple[str, str]], alias: str) -> str:
    return next(model for name, model in left_models if name == alias)


def _relationship_condition(relationship: Relationship, aliases: dict[str, str]) -> exp.Expr:
    condition = _parse_expression(relationship.condition)
    for column in list(condition.find_all(exp.Column)):
        alias = aliases.get(column.table.lower())
        if alias is not None:
            column.set("table", exp.to_identifier(alias))
    return condition
