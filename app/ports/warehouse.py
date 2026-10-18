"""Port for running validated read-only queries on the PostgreSQL warehouse."""

from typing import Protocol

from app.core.types import QueryResult, ValidatedQuery


class WarehousePort(Protocol):
    """Read-only warehouse access."""
    def execute_read_only(
        self,
        query: ValidatedQuery,
        *,
        max_rows: int,
    ) -> QueryResult:
        """Run read-only; return at most ``max_rows`` rows, setting ``truncated`` if cut."""
        ...

    def explain(self, query: ValidatedQuery) -> None:
        """Dry run: plan the query without executing it. Raises when PostgreSQL rejects it."""
        ...
