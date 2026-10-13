"""Compiled SQL must actually run on PostgreSQL and enforce policies (needs WAREHOUSE_DSN)."""

from __future__ import annotations

import os

import pytest

from app.adapters.warehouse.postgres import PostgresWarehouseAdapter
from app.core.types import ValidatedQuery
from app.semantic.compiler import compile_query
from tests.support.ecommerce_mdl import (
    build_mdl,
    build_policies,
    build_principal,
    build_users,
    load_cases,
)

OK_CASES = [case for case in load_cases()["cases"] if case["expect"]["status"] == "ok"]


@pytest.fixture(scope="module")
def warehouse() -> PostgresWarehouseAdapter:
    dsn = os.getenv("WAREHOUSE_DSN")
    if not dsn:
        pytest.skip("WAREHOUSE_DSN is required for live compiler tests")
    return PostgresWarehouseAdapter(dsn, statement_timeout_ms=5_000, max_rows=100)


def run(warehouse: PostgresWarehouseAdapter, sql: str, user: str | None, attrs: dict | None = None):  # type: ignore[no-untyped-def]
    principal = build_users().get(user) if user else None
    if attrs is not None:
        principal = build_principal("live", attrs)
    compiled = compile_query(sql, build_mdl(), principal, build_policies())
    assert compiled.ok, compiled.errors
    return warehouse.execute_read_only(
        ValidatedQuery(sql=compiled.sql, parameters=compiled.parameters), max_rows=100
    )


@pytest.mark.parametrize("case", OK_CASES, ids=[case["id"] for case in OK_CASES])
def test_every_ok_case_executes_on_postgres(case: dict, warehouse: PostgresWarehouseAdapter) -> None:
    run(warehouse, case["logical_sql"], case["user"])


def test_row_policy_restricts_rows_to_the_users_region(warehouse: PostgresWarehouseAdapter) -> None:
    everyone = run(warehouse, "SELECT DISTINCT region FROM orders", None)
    assert {row[0] for row in everyone.rows} >= {"West", "South"}
    west = run(warehouse, "SELECT DISTINCT region FROM orders", None, {"region": "West"})
    assert {row[0] for row in west.rows} == {"West"}


def test_policy_applies_inside_subqueries_and_joins(warehouse: PostgresWarehouseAdapter) -> None:
    joined = run(
        warehouse,
        "SELECT DISTINCT c.region FROM orders o JOIN customers c ON c.customer_id = o.customer_id",
        None,
        {"region": "South"},
    )
    assert {row[0] for row in joined.rows} <= {"South"}
    nested = run(
        warehouse,
        "SELECT COUNT(*) FROM customers WHERE customer_id IN (SELECT customer_id FROM orders)",
        None,
        {"region": "nowhere"},
    )
    assert nested.rows == ((0,),)


def test_missing_attribute_returns_no_rows(warehouse: PostgresWarehouseAdapter) -> None:
    assert run(warehouse, "SELECT order_id FROM orders", None, {}).rows == ()


def test_list_attribute_uses_any(warehouse: PostgresWarehouseAdapter) -> None:
    both = run(warehouse, "SELECT DISTINCT warehouse_region FROM inventory", None, {"regions": ["West", "South"]})
    only_west = run(warehouse, "SELECT DISTINCT warehouse_region FROM inventory", None, {"regions": ["West"]})
    assert {row[0] for row in only_west.rows} <= {"West"} <= {row[0] for row in both.rows} | {"West"}


def test_literal_percent_signs_survive_parameterised_execution(warehouse: PostgresWarehouseAdapter) -> None:
    result = run(
        warehouse,
        "SELECT order_id % 2 AS parity FROM orders WHERE status LIKE 'ship%' OR status LIKE 'deliv%'",
        None,
    )
    assert result.columns == ("parity",)
