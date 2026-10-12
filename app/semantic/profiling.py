import json
import re
from dataclasses import dataclass

from pydantic import BaseModel

from app.core.catalog import CatalogTable, CatalogTableProfile
from app.domain.mdl import (
    JoinType,
    Mdl,
    MdlColumn,
    MdlModel,
    Relationship,
    RelationshipSource,
    validate_mdl_against_catalog,
)
from app.ports.catalog import CatalogPort
from app.ports.llm import LLMPort


class TableSample(BaseModel):
    schema_name: str
    table_name: str
    row_count: int
    sample_rows: list[dict[str, object]]


class ProfilingSummary(BaseModel):
    selected_tables: int
    total_rows: int
    models_needing_review: list[str]


class ProfiledDraft(BaseModel):
    mdl: Mdl
    tables: list[TableSample]
    summary: ProfilingSummary


@dataclass(frozen=True)
class _ModelMapping:
    logical_name: str
    profile: CatalogTableProfile
    physical_to_logical_column: dict[str, str]


class ProfilingService:
    def __init__(self, catalog: CatalogPort, llm: LLMPort | None = None) -> None:
        self._catalog = catalog
        self._llm = llm

    def list_tables(self, schemas: tuple[str, ...] = ()) -> tuple[CatalogTable, ...]:
        return self._catalog.list_tables(schemas)

    def create_draft(
        self,
        tables: tuple[tuple[str, str], ...],
        *,
        sample_limit: int = 3,
    ) -> ProfiledDraft:
        if not tables:
            raise ValueError("Select at least one table to profile")
        if len(set(tables)) != len(tables):
            raise ValueError("Selected tables must be unique")

        profiles = self._catalog.profile_tables(tables, sample_limit=sample_limit)
        mappings = self._create_model_mappings(profiles)
        models = [self._to_mdl_model(mapping) for mapping in mappings]
        relationships = self._suggest_relationships(mappings)
        mdl = Mdl(models=models, relationships=relationships)
        validate_mdl_against_catalog(
            mdl,
            {
                f"{profile.table.schema_name}.{profile.table.table_name}": {
                    column.name: column.data_type for column in profile.columns
                }
                for profile in profiles
            },
        )
        profile_by_key = {
            (profile.table.schema_name, profile.table.table_name): profile for profile in profiles
        }
        if set(profile_by_key) != set(tables):
            raise RuntimeError("Catalog returned duplicate or missing selected table profiles")

        table_reports = [
            TableSample(
                schema_name=profile.table.schema_name,
                table_name=profile.table.table_name,
                row_count=profile.row_count,
                sample_rows=[dict(row) for row in profile.sample_rows],
            )
            for profile in profiles
        ]
        return ProfiledDraft(
            mdl=mdl,
            tables=table_reports,
            summary=ProfilingSummary(
                selected_tables=len(profiles),
                total_rows=sum(profile.row_count for profile in profiles),
                models_needing_review=[model.name for model in mdl.models if model.needs_review],
            ),
        )

    def draft_descriptions(self, mdl: Mdl) -> Mdl:
        if self._llm is None:
            raise RuntimeError("Local description model is not configured")
        input_schema = [
            {
                "model": model.name,
                "table": model.table_reference,
                "columns": [
                    {"name": column.name, "type": column.type}
                    for column in model.columns
                    if not column.is_calculated
                ],
            }
            for model in mdl.models
        ]
        response = self._llm.generate(
            "Write concise, factual business-friendly descriptions using only table "
            "and column names and types. Do not invent meanings. Never request or "
            "include sample data or credentials. Return JSON shaped like "
            '{"models":[{"name":"...","description":"...","columns":'
            '[{"name":"...","description":"..."}]}]}. Every input model and '
            "column must appear exactly once.\n"
            + json.dumps(input_schema, ensure_ascii=True)
        )
        try:
            decoded = json.loads(response)
        except json.JSONDecodeError as error:
            raise ValueError("Local model returned invalid description JSON") from error
        if not isinstance(decoded, dict) or not isinstance(decoded.get("models"), list):
            raise TypeError("Local model response must include a models array")
        generated_models: dict[str, dict[str, object]] = {}
        for item in decoded["models"]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise TypeError("Local model returned a malformed model description")
            name = item["name"]
            if name in generated_models:
                raise ValueError(f"Local model repeated model description {name!r}")
            generated_models[name] = item

        expected_models = {model.name for model in mdl.models}
        if generated_models.keys() != expected_models:
            raise ValueError("Local model must describe exactly the requested models")
        for model in mdl.models:
            generated = generated_models[model.name]
            description = generated.get("description")
            generated_columns = generated.get("columns")
            if not isinstance(description, str) or not isinstance(generated_columns, list):
                raise TypeError(f"Local model returned an incomplete description for {model.name}")
            descriptions_by_column: dict[str, str] = {}
            for column in generated_columns:
                if (
                    not isinstance(column, dict)
                    or not isinstance(column.get("name"), str)
                    or not isinstance(column.get("description"), str)
                ):
                    raise TypeError(
                        f"Local model returned a malformed column description for {model.name}"
                    )
                column_name = column["name"]
                if column_name in descriptions_by_column:
                    raise ValueError(f"Local model repeated column description {column_name!r}")
                descriptions_by_column[column_name] = column["description"]
            physical_columns = {
                column.name for column in model.columns if not column.is_calculated
            }
            if descriptions_by_column.keys() != physical_columns:
                raise ValueError(f"Local model must describe exactly the columns in {model.name}")
            model.description = description.strip()
            model.needs_review = True
            for column in model.columns:
                if not column.is_calculated:
                    column.description = descriptions_by_column[column.name].strip()
                column.needs_review = True
        return mdl

    def _create_model_mappings(
        self,
        profiles: tuple[CatalogTableProfile, ...],
    ) -> list[_ModelMapping]:
        use_schema_prefix = len({profile.table.table_name for profile in profiles}) != len(profiles)
        used_model_names: set[str] = set()
        mappings: list[_ModelMapping] = []
        for profile in profiles:
            physical_table = profile.table.table_name
            base_name = _safe_name(
                f"{profile.table.schema_name}_{physical_table}"
                if use_schema_prefix
                else physical_table
            )
            logical_name = _unique_name(base_name, used_model_names)
            used_columns: set[str] = set()
            column_map = {
                column.name: _unique_name(_safe_name(column.name), used_columns)
                for column in profile.columns
            }
            mappings.append(_ModelMapping(logical_name, profile, column_map))
        return mappings

    @staticmethod
    def _to_mdl_model(mapping: _ModelMapping) -> MdlModel:
        profile = mapping.profile
        columns = [
            MdlColumn(
                name=mapping.physical_to_logical_column[column.name],
                type=column.data_type,
                physicalName=(
                    column.name
                    if mapping.physical_to_logical_column[column.name] != column.name
                    else None
                ),
                isPrimaryKey=column.is_primary_key,
            )
            for column in profile.columns
        ]
        primary_key_columns = [
            mapping.physical_to_logical_column[column.name]
            for column in profile.columns
            if column.is_primary_key
        ]
        primary_key = primary_key_columns[0] if len(primary_key_columns) == 1 else None
        return MdlModel(
            name=mapping.logical_name,
            tableReference=f"{profile.table.schema_name}.{profile.table.table_name}",
            primaryKey=primary_key,
            columns=columns,
            approxRows=profile.row_count,
        )

    @staticmethod
    def _suggest_relationships(
        mappings: list[_ModelMapping],
    ) -> list[Relationship]:
        model_by_physical_table = {
            (mapping.profile.table.schema_name, mapping.profile.table.table_name): mapping
            for mapping in mappings
        }
        suggestions: list[Relationship] = []
        used_names: set[str] = set()
        existing_pairs: set[tuple[str, str, str]] = set()

        for mapping in mappings:
            for foreign_key in mapping.profile.foreign_keys:
                referenced = model_by_physical_table.get(
                    (foreign_key.referenced_schema, foreign_key.referenced_table)
                )
                if referenced is None or referenced.logical_name == mapping.logical_name:
                    continue
                if len(foreign_key.columns) != len(foreign_key.referenced_columns):
                    raise ValueError(f"Foreign key {foreign_key.name!r} has mismatched columns")
                condition = " AND ".join(
                    f"{mapping.logical_name}.{mapping.physical_to_logical_column[local]} = "
                    f"{referenced.logical_name}."
                    f"{referenced.physical_to_logical_column[remote]}"
                    for local, remote in zip(
                        foreign_key.columns,
                        foreign_key.referenced_columns,
                        strict=True,
                    )
                )
                pair_key = (
                    mapping.logical_name,
                    referenced.logical_name,
                    condition,
                )
                if pair_key in existing_pairs:
                    continue
                existing_pairs.add(pair_key)
                name = _unique_name(
                    _safe_name(
                        f"{mapping.logical_name}_{referenced.logical_name}_{foreign_key.name}"
                    ),
                    used_names,
                )
                suggestions.append(
                    Relationship(
                        name=name,
                        models=(mapping.logical_name, referenced.logical_name),
                        joinType=JoinType.MANY_TO_ONE,
                        condition=condition,
                        source=RelationshipSource.DATABASE_FOREIGN_KEY,
                    )
                )

        foreign_key_columns = {
            (mapping.logical_name, column)
            for mapping in mappings
            for foreign_key in mapping.profile.foreign_keys
            for column in foreign_key.columns
        }
        for left in mappings:
            for right in mappings:
                if left.logical_name == right.logical_name:
                    continue
                right_primary_keys = {
                    column.name for column in right.profile.columns if column.is_primary_key
                }
                for left_column in left.profile.columns:
                    if (
                        not left_column.name.lower().endswith("_id")
                        or (left.logical_name, left_column.name) in foreign_key_columns
                        or left_column.name not in right_primary_keys
                    ):
                        continue
                    condition = (
                        f"{left.logical_name}."
                        f"{left.physical_to_logical_column[left_column.name]} = "
                        f"{right.logical_name}."
                        f"{right.physical_to_logical_column[left_column.name]}"
                    )
                    pair_key = (left.logical_name, right.logical_name, condition)
                    if pair_key in existing_pairs:
                        continue
                    existing_pairs.add(pair_key)
                    name = _unique_name(
                        _safe_name(f"{left.logical_name}_{right.logical_name}_inferred"),
                        used_names,
                    )
                    suggestions.append(
                        Relationship(
                            name=name,
                            models=(left.logical_name, right.logical_name),
                            joinType=JoinType.MANY_TO_ONE,
                            condition=condition,
                            source=RelationshipSource.INFERRED_COLUMN_NAME,
                            needsReview=True,
                        )
                    )
        return suggestions


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    if not normalized or not normalized[0].isalpha():
        normalized = f"model_{normalized}" if normalized else "model"
    if not normalized[0].islower():
        normalized = normalized.lower()
    return normalized


def _unique_name(base: str, used: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate
