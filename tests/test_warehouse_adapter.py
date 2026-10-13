from contextlib import nullcontext
from pathlib import Path

import pytest

import app.adapters.warehouse.postgres as warehouse_module
import app.adapters.warehouse.postgres_catalog as catalog_module
from app.adapters.warehouse.postgres import (
    PostgresWarehouseAdapter,
    UnsafeWarehouseQuery,
)
from app.adapters.warehouse.postgres_catalog import PostgresCatalogAdapter
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


def test_adapter_reports_when_results_are_truncated(monkeypatch) -> None:
    class Cursor:
        description = (type("Column", (), {"name": "id"})(),)

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, *_args: object) -> None:
            return None

        def fetchmany(self, size: int) -> list[tuple[int]]:
            assert size == 3
            return [(1,), (2,), (3,)]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> Cursor:
            return Cursor()

    monkeypatch.setattr(warehouse_module.psycopg, "connect", lambda *_args: Connection())
    result = PostgresWarehouseAdapter("dbname=demo", max_rows=2).execute_read_only(
        ValidatedQuery(sql="SELECT id FROM ecommerce.customers", parameters={}),
        max_rows=2,
    )

    assert result.rows == ((1,), (2,))
    assert result.truncated is True


def test_catalog_adapter_uses_a_private_temporary_ca_file(monkeypatch) -> None:
    certificate = "-----BEGIN CERTIFICATE-----\nlocal-test\n-----END CERTIFICATE-----\n"
    created_paths: list[Path] = []

    def connect(conninfo: str):
        from psycopg.conninfo import conninfo_to_dict

        path = Path(conninfo_to_dict(conninfo)["sslrootcert"])
        assert path.read_text(encoding="utf-8") == certificate
        assert path.stat().st_mode & 0o077 == 0
        created_paths.append(path)
        return nullcontext(object())

    monkeypatch.setattr(catalog_module.psycopg, "connect", connect)
    catalog = PostgresCatalogAdapter(
        "host=localhost dbname=demo",
        ssl_ca_certificate=certificate,
    )

    with catalog._connect():
        assert created_paths[0].exists()

    assert not created_paths[0].exists()

