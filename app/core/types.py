"""Core value types shared across the application."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeAlias

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)


@dataclass(frozen=True)
class ValidatedQuery:
    """SQL that passed the semantic compiler, with its bound parameters."""
    sql: str
    parameters: Mapping[str, object]


@dataclass(frozen=True)
class Principal:
    """The authenticated caller: role, capabilities, projects and attributes for policies."""
    user_id: str
    role_name: str
    capabilities: frozenset[str]
    project_ids: frozenset[str]
    attributes: Mapping[str, JsonValue]


@dataclass(frozen=True)
class AuditEvent:
    """An action to record in the audit trail."""
    actor_id: str | None
    action: str
    resource_type: str
    resource_id: str
    details: Mapping[str, object]


@dataclass(frozen=True)
class QueryResult:
    """Warehouse rows with column names; ``truncated`` is set when cut at the row limit."""
    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    truncated: bool = False


@dataclass(frozen=True)
class VectorRecord:
    """A vector to store, with its id and payload."""
    record_id: str
    vector: tuple[float, ...]
    payload: Mapping[str, JsonValue]


@dataclass(frozen=True)
class VectorMatch:
    """A search hit: record id, similarity score and stored payload."""
    record_id: str
    score: float
    payload: Mapping[str, JsonValue]