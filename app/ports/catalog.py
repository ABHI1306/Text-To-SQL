"""Port for introspecting the warehouse's tables, columns and sample data."""

from typing import Protocol

from app.core.catalog import CatalogTable, CatalogTableProfile


class CatalogPort(Protocol):
    """Read-only view of the warehouse structure used to build the semantic model."""

    def list_tables(self, schemas: tuple[str, ...] = ()) -> tuple[CatalogTable, ...]:
        """List tables in ``schemas`` (all non-system schemas when empty)."""
        ...

    def profile_tables(
        self,
        tables: tuple[tuple[str, str], ...],
        *,
        sample_limit: int = 3,
    ) -> tuple[CatalogTableProfile, ...]:
        """Profile each ``(schema, table)``: columns, keys, row count and sample rows."""
        ...
