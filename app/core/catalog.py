"""Value types describing the warehouse catalog."""

from dataclasses import dataclass
from typing import TypeAlias

from app.core.types import JsonValue


@dataclass(frozen=True)
class CatalogTable:
    """A table or view with its approximate size."""
    schema_name: str
    table_name: str
    table_type: str
    approx_rows: int = 0
    column_count: int = 0


@dataclass(frozen=True)
class CatalogColumn:
    """A table column: type, nullability and primary-key membership."""
    name: str
    data_type: str
    nullable: bool
    is_primary_key: bool


@dataclass(frozen=True)
class CatalogForeignKey:
    """A foreign key from local columns to columns of another table."""
    name: str
    columns: tuple[str, ...]
    referenced_schema: str
    referenced_table: str
    referenced_columns: tuple[str, ...]


SampleRow: TypeAlias = dict[str, JsonValue]


@dataclass(frozen=True)
class CatalogTableProfile:
    """A table with its columns, foreign keys, row count and sample rows."""
    table: CatalogTable
    columns: tuple[CatalogColumn, ...]
    foreign_keys: tuple[CatalogForeignKey, ...]
    row_count: int
    sample_rows: tuple[SampleRow, ...]
