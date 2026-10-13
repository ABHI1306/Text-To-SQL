from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeAlias

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)


@dataclass(frozen=True)
class ValidatedQuery:
    sql: str
    parameters: Mapping[str, object]


@dataclass(frozen=True)
class Principal:
    user_id: str
    role_name: str
    capabilities: frozenset[str]
    project_ids: frozenset[str]
    attributes: Mapping[str, JsonValue]


@dataclass(frozen=True)
class AuditEvent:
    actor_id: str | None
    action: str
    resource_type: str
    resource_id: str
    details: Mapping[str, object]


@dataclass(frozen=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    truncated: bool = False


@dataclass(frozen=True)
class VectorRecord:
    record_id: str
    vector: tuple[float, ...]
    payload: Mapping[str, JsonValue]


@dataclass(frozen=True)
class VectorMatch:
    record_id: str
    score: float
    payload: Mapping[str, JsonValue]