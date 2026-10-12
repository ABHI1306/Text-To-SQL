import pytest

from app.adapters.warehouse.postgres import (
    PostgresWarehouseAdapter,
    UnsafeWarehouseQuery,
)
from app.core.types import ValidatedQuery


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE orders SET status = 'paid'",
        "DELETE FROM orders",
        "INSERT INTO orders (id) VALUES (1)",
        "SELECT 1; SELECT 2",
        "SELECT * FROM orders FOR UPDATE",
        "SELECT 1 INTO new_table",
        "WITH changed AS (DELETE FROM orders RETURNING *) SELECT * FROM changed",
    ],
)
def test_rejects_non_read_only_or_multiple_statements(sql: str) -> None:
    with pytest.raises(UnsafeWarehouseQuery):
        PostgresWarehouseAdapter._ensure_read_only_select(sql)


def test_accepts_single_select() -> None:
    PostgresWarehouseAdapter._ensure_read_only_select("SELECT 1")


def test_warehouse_adapter_requires_dsn() -> None:
    with pytest.raises(ValueError, match="DSN"):
        PostgresWarehouseAdapter("")


def test_adapter_enforces_configured_row_cap() -> None:
    adapter = PostgresWarehouseAdapter("dbname=demo", max_rows=25)

    with pytest.raises(ValueError, match="between 1 and 25"):
        adapter.execute_read_only(
            ValidatedQuery(sql="SELECT 1", parameters={}),
            max_rows=26,
        )
