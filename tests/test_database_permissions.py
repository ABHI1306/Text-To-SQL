import os
import uuid

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege, QueryCanceled

from app.adapters.warehouse.postgres import PostgresWarehouseAdapter
from app.core.types import ValidatedQuery


def _required_dsn(name: str) -> str:
    dsn = os.getenv(name)
    if not dsn:
        pytest.skip(f"{name} is required for the live database permission test")
    return dsn


def test_warehouse_adapter_runs_select_one() -> None:
    adapter = PostgresWarehouseAdapter(
        _required_dsn("WAREHOUSE_DSN"),
        statement_timeout_ms=2_000,
        max_rows=10,
    )

    result = adapter.execute_read_only(
        ValidatedQuery(sql="SELECT 1 AS ready", parameters={}),
        max_rows=10,
    )

    assert result.columns == ("ready",)
    assert result.rows == ((1,),)


def test_warehouse_adapter_enforces_row_cap() -> None:
    adapter = PostgresWarehouseAdapter(
        _required_dsn("WAREHOUSE_DSN"),
        statement_timeout_ms=2_000,
        max_rows=5,
    )

    result = adapter.execute_read_only(
        ValidatedQuery(sql="SELECT generate_series(1, 100) AS value", parameters={}),
        max_rows=5,
    )

    assert result.rows == ((1,), (2,), (3,), (4,), (5,))


def test_warehouse_adapter_enforces_statement_timeout() -> None:
    adapter = PostgresWarehouseAdapter(
        _required_dsn("WAREHOUSE_DSN"),
        statement_timeout_ms=100,
        max_rows=10,
    )

    with pytest.raises(QueryCanceled):
        adapter.execute_read_only(
            ValidatedQuery(sql="SELECT pg_sleep(2)", parameters={}),
            max_rows=10,
        )


def test_warehouse_role_cannot_write() -> None:
    with (
        psycopg.connect(_required_dsn("WAREHOUSE_DSN"), autocommit=True) as connection,
        pytest.raises(InsufficientPrivilege),
    ):
        connection.execute("DELETE FROM ecommerce.orders WHERE false")


def test_metadata_audit_role_is_append_only() -> None:
    dsn = _required_dsn("METADATA_APP_DSN")
    resource_id = str(uuid.uuid4())

    with psycopg.connect(dsn, autocommit=True) as connection:
        event_id = connection.execute(
            """
            INSERT INTO audit_events (action, resource_type, resource_id)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            ("permission_test", "test", resource_id),
        ).fetchone()[0]

        with pytest.raises(InsufficientPrivilege):
            connection.execute(
                "UPDATE audit_events SET action = %s WHERE id = %s",
                ("tampered", event_id),
            )
        with pytest.raises(InsufficientPrivilege):
            connection.execute("DELETE FROM audit_events WHERE id = %s", (event_id,))
