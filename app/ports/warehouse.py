from typing import Protocol

from app.core.types import QueryResult, ValidatedQuery


class WarehousePort(Protocol):
    def execute_read_only(
        self,
        query: ValidatedQuery,
        *,
        max_rows: int,
    ) -> QueryResult: ...

    def explain(self, query: ValidatedQuery) -> None:
        """Dry run: plan the query without executing it. Raises when PostgreSQL rejects it."""
        ...
