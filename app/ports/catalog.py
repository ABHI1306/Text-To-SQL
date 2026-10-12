from typing import Protocol

from app.core.catalog import CatalogTable, CatalogTableProfile


class CatalogPort(Protocol):
    def list_tables(self, schemas: tuple[str, ...] = ()) -> tuple[CatalogTable, ...]: ...

    def profile_tables(
        self,
        tables: tuple[tuple[str, str], ...],
        *,
        sample_limit: int = 3,
    ) -> tuple[CatalogTableProfile, ...]: ...
