"""Semantic model (MDL) documents and their validation rules."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
LARGE_TABLE_ROWS = 100_000


class MdlValidationError(ValueError):
    """Raised when semantic-model contents violate domain rules."""


class JoinType(StrEnum):
    """Cardinality of a relationship between two models."""
    ONE_TO_ONE = "ONE_TO_ONE"
    ONE_TO_MANY = "ONE_TO_MANY"
    MANY_TO_ONE = "MANY_TO_ONE"
    MANY_TO_MANY = "MANY_TO_MANY"


class RelationshipSource(StrEnum):
    """How a relationship was found: a database foreign key or a column-name guess."""
    DATABASE_FOREIGN_KEY = "database_foreign_key"
    INFERRED_COLUMN_NAME = "inferred_column_name"


class MdlModel(BaseModel):
    """A semantic model over one physical table, with its columns."""
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str
    table_reference: str = Field(alias="tableReference")
    description: str = ""
    primary_key: str | None = Field(default=None, alias="primaryKey")
    columns: list[MdlColumn]
    approx_rows: int = Field(default=0, ge=0, alias="approxRows")
    needs_review: bool = Field(default=False, alias="needsReview")

    @model_validator(mode="after")
    def mark_for_review(self) -> Self:
        """Flag the model for review when undescribed, or large with undescribed columns."""
        if not self.description.strip() or (
            self.approx_rows >= LARGE_TABLE_ROWS
            and any(not column.description.strip() for column in self.columns)
        ):
            self.needs_review = True
        return self


class Relationship(BaseModel):
    """A join between two distinct models, as a SQL condition."""
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str
    models: tuple[str, str]
    join_type: JoinType = Field(alias="joinType")
    condition: str
    source: RelationshipSource = RelationshipSource.DATABASE_FOREIGN_KEY
    needs_review: bool = Field(default=False, alias="needsReview")

    @model_validator(mode="after")
    def require_distinct_models(self) -> Self:
        """Reject a relationship that joins a model to itself."""
        if self.models[0] == self.models[1]:
            raise ValueError("a relationship must reference two distinct models")
        return self


class MetricField(BaseModel):
    """A named SQL expression used as a metric dimension or measure."""
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str
    expression: str
    description: str = ""


class Metric(BaseModel):
    """A business metric defined by dimensions and measures over one base model."""
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str
    base_object: str = Field(alias="baseObject")
    dimensions: list[MetricField] = Field(default_factory=list)
    measures: list[MetricField] = Field(default_factory=list)
    description: str = ""


class Mdl(BaseModel):
    """A whole semantic-model document; construction validates names and references."""
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    version: int = Field(default=3, ge=1)
    dialect: str = "postgresql"
    models: list[MdlModel]
    relationships: list[Relationship] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    layout: dict[str, dict[str, float]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_semantic_references(self) -> Self:
        """Check unique names, column expressions, relationships and metrics."""
        self._validate_unique_names()
        model_by_name = {model.name: model for model in self.models}
        self._validate_calculated_columns(model_by_name)
        self._validate_relationships(model_by_name)
        self._validate_metrics(model_by_name)
        return self

    def _validate_unique_names(self) -> None:
        """Require valid, unique names for models, relationships, metrics and their fields."""
        name_groups = {
            "model": [model.name for model in self.models],
            "relationship": [relationship.name for relationship in self.relationships],
            "metric": [metric.name for metric in self.metrics],
        }
        for kind, names in name_groups.items():
            _require_unique_valid_names(names, kind)
        for model in self.models:
            _require_unique_valid_names(
                [column.name for column in model.columns],
                f"column in model {model.name}",
            )
        for metric in self.metrics:
            _require_unique_valid_names(
                [field.name for field in metric.dimensions + metric.measures],
                f"metric field in metric {metric.name}",
            )

    def _validate_calculated_columns(self, models: dict[str, MdlModel]) -> None:
        """Check primary keys name real columns and calculated expressions use only own columns."""
        for model in self.models:
            columns = {column.name for column in model.columns}
            if model.primary_key and model.primary_key not in columns:
                raise ValueError(
                    f"model {model.name}: primaryKey {model.primary_key!r} is not a column"
                )
            for column in model.columns:
                if column.is_primary_key and model.primary_key not in (None, column.name):
                    raise ValueError(f"model {model.name}: conflicting primary key declarations")
                if column.is_calculated:
                    _validate_expression(
                        column.expression or "",
                        allowed_columns=columns,
                        field_path=f"models.{model.name}.columns.{column.name}.expression",
                    )

    def _validate_relationships(self, models: dict[str, MdlModel]) -> None:
        """Check each relationship uses known models and qualified, known columns."""
        for relationship in self.relationships:
            missing = set(relationship.models) - models.keys()
            if missing:
                raise ValueError(
                    f"relationship {relationship.name}: unknown model(s) {sorted(missing)}"
                )
            allowed = {
                model_name: {column.name for column in models[model_name].columns}
                for model_name in relationship.models
            }
            _validate_relationship_condition(
                relationship.condition,
                allowed_models=allowed,
                field_path=f"relationships.{relationship.name}.condition",
            )

    def _validate_metrics(self, models: dict[str, MdlModel]) -> None:
        """Check each metric's base model exists and its expressions use only its columns."""
        for metric in self.metrics:
            model = models.get(metric.base_object)
            if model is None:
                raise ValueError(f"metric {metric.name}: unknown baseObject {metric.base_object!r}")
            allowed_columns = {column.name for column in model.columns}
            for field in metric.dimensions + metric.measures:
                _validate_expression(
                    field.expression,
                    allowed_columns=allowed_columns,
                    field_path=f"metrics.{metric.name}.{field.name}.expression",
                )


class MdlColumn(BaseModel):
    """A column of a model: physical, or calculated from an expression over other columns."""
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str
    type: str
    physical_name: str | None = Field(default=None, alias="physicalName")
    description: str = ""
    is_primary_key: bool = Field(default=False, alias="isPrimaryKey")
    is_calculated: bool = Field(default=False, alias="isCalculated")
    expression: str | None = None
    needs_review: bool = Field(default=False, alias="needsReview")

    @model_validator(mode="after")
    def validate_calculation_and_review(self) -> Self:
        """Require an expression exactly for calculated columns; flag undescribed columns."""
        if self.is_calculated != (self.expression is not None):
            raise ValueError(
                "calculated columns require an expression and physical columns forbid one"
            )
        if not self.description.strip():
            self.needs_review = True
        return self


def _require_unique_valid_names(names: list[str], kind: str) -> None:
    """Raise ``ValueError`` for a name outside ``NAME_PATTERN`` or repeated in ``names``."""
    seen: set[str] = set()
    for name in names:
        if not NAME_PATTERN.fullmatch(name):
            raise ValueError(f"{kind} name {name!r} must match [a-z][a-z0-9_]*")
        if name in seen:
            raise ValueError(f"duplicate {kind} name {name!r}")
        seen.add(name)


def _parse_expression(expression: str, field_path: str) -> exp.Expr:
    """Parse a PostgreSQL expression, rejecting syntax errors and whole queries."""
    try:
        parsed = parse_one(expression, read="postgres")
    except ParseError as error:
        raise ValueError(f"{field_path}: invalid PostgreSQL expression") from error
    if isinstance(parsed, (exp.Query, exp.Command)):
        raise ValueError(f"{field_path}: expected an expression, not a query")  # noqa: TRY004
    return parsed


def _validate_expression(
    expression: str,
    *,
    allowed_columns: set[str],
    field_path: str,
) -> None:
    """Require ``expression`` to parse and reference only ``allowed_columns``."""
    parsed = _parse_expression(expression, field_path)
    referenced = {column.name.lower() for column in parsed.find_all(exp.Column)}
    unknown = referenced - allowed_columns
    if unknown:
        raise ValueError(f"{field_path}: unknown column(s) {sorted(unknown)}")


def _validate_relationship_condition(
    condition: str,
    *,
    allowed_models: dict[str, set[str]],
    field_path: str,
) -> None:
    """Require a boolean condition whose columns are qualified by an allowed model and exist."""
    parsed = _parse_expression(condition, field_path)
    if not any(isinstance(node, exp.Predicate) for node in parsed.walk()):
        raise ValueError(f"{field_path}: expected a boolean join condition")
    for column in parsed.find_all(exp.Column):
        model_name = column.table.lower()
        if (
            model_name not in allowed_models
            or column.name.lower() not in allowed_models[model_name]
        ):
            raise ValueError(f"{field_path}: unknown or unqualified reference {column.sql()!r}")


def validate_mdl_against_catalog(
    mdl: Mdl,
    catalog: dict[str, dict[str, str]],
) -> None:
    """Ensure physical references and column types exist in introspected catalog data."""
    for model in mdl.models:
        physical_columns = catalog.get(model.table_reference)
        if physical_columns is None:
            raise MdlValidationError(
                f"models.{model.name}.tableReference: table {model.table_reference!r} does not exist"
            )
        for column in model.columns:
            if column.is_calculated:
                continue
            physical_name = column.physical_name or column.name
            actual_type = physical_columns.get(physical_name)
            if actual_type is None:
                raise MdlValidationError(
                    f"models.{model.name}.columns.{column.name}: "
                    f"column does not exist (physical name {physical_name!r})"
                )
            if not _compatible_type(column.type, actual_type):
                raise MdlValidationError(
                    f"models.{model.name}.columns.{column.name}.type: "
                    f"{column.type!r} is incompatible with {actual_type!r}"
                )


def _compatible_type(model_type: str, database_type: str) -> bool:
    """Whether a declared type and a database type are equal or aliases of one another."""
    groups = (
        {"int2", "smallint", "smallserial"},
        {"int4", "integer", "serial"},
        {"int8", "bigint", "bigserial"},
        {"float4", "real"},
        {"float8", "double precision"},
        {"varchar", "character varying", "text", "string"},
        {"bool", "boolean"},
        {"decimal", "numeric"},
        {"timestamp", "timestamp without time zone"},
        {"timestamptz", "timestamp with time zone"},
    )
    left = model_type.strip().lower()
    right = database_type.strip().lower()
    return left == right or any(left in group and right in group for group in groups)
