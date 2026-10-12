from dataclasses import dataclass
from typing import TypeAlias

from app.core.types import JsonValue


@dataclass(frozen=True)
class CatalogTable:
    schema_name: str
    table_name: str
    table_type: str
    approx_rows: int = 0
    column_count: int = 0


@dataclass(frozen=True)
class CatalogColumn:
    name: str
    data_type: str
    nullable: bool
    is_primary_key: bool


@dataclass(frozen=True)
class CatalogForeignKey:
    name: str
    columns: tuple[str, ...]
    referenced_schema: str
    referenced_table: str
    referenced_columns: tuple[str, ...]


SampleRow: TypeAlias = dict[str, JsonValue]


@dataclass(frozen=True)
class CatalogTableProfile:
    table: CatalogTable
    columns: tuple[CatalogColumn, ...]
    foreign_keys: tuple[CatalogForeignKey, ...]
    row_count: int
    sample_rows: tuple[SampleRow, ...]
