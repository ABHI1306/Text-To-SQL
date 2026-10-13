"""Row-level security suite: 3 users x every query shape, against PostgreSQL (needs WAREHOUSE_DSN).

For each shape the oracle is an independent, hand-written physical query that reads the two
policed tables through explicit region filters. A user may see exactly the oracle's rows, no
more and no fewer. Shapes the compiler must refuse are expected to fail to compile.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from app.adapters.warehouse.postgres import PostgresWarehouseAdapter
from app.core.types import ValidatedQuery
from app.semantic.compiler import compile_query
from app.semantic.validator import validate_compiled_sql
from tests.support.ecommerce_mdl import build_mdl, build_policies, build_principal, load_cases

OVERLAY = load_cases()["model_overlay"]
OVERLAY["metrics"] = [
    *OVERLAY["metrics"],
    {
        "name": "orders_by_status",
        "base_model": "orders",
        "dimensions": [{"name": "status", "expression": "status"}],
        "measures": [{"name": "n", "expression": "count(*)"}],
    },
]
MDL = build_mdl(OVERLAY)
POLICIES = build_policies(OVERLAY)

USERS = {
    "west": build_principal("west", {"region": "West"}),
    "south": build_principal("south", {"region": "South"}),
    "no_region": build_principal("no_region", {}),
}
REGION = {"west": "West", "south": "South", "no_region": None}

PRE = (
    "WITH o AS (SELECT * FROM ecommerce.orders WHERE region = %(r)s), "
    "c AS (SELECT * FROM ecommerce.customers WHERE region = %(r)s) "
)
ERR = "error"

# (id, logical SQL a user could write, oracle physical SQL over o/c  or  ERR)
SHAPES: list[tuple[str, str, str]] = [
    ("direct-orders", "SELECT order_id, region FROM orders", "SELECT order_id, region FROM o"),
    ("direct-customers", "SELECT customer_id, email FROM customers", "SELECT customer_id, email FROM c"),
    ("star", "SELECT * FROM orders", "SELECT order_id, customer_id, ordered_at, status, region FROM o"),
    ("count", "SELECT COUNT(*) FROM orders", "SELECT COUNT(*) FROM o"),
    ("quoted", 'SELECT "order_id" FROM "orders"', "SELECT order_id FROM o"),
    ("uppercase", "SELECT ORDER_ID FROM ORDERS", "SELECT order_id FROM o"),
    (
        "join-items",
        "SELECT SUM(oi.quantity) FROM order_items oi JOIN orders o ON o.order_id = oi.order_id",
        "SELECT SUM(oi.quantity) FROM ecommerce.order_items oi JOIN o ON o.order_id = oi.order_id",
    ),
    ("alias", "SELECT x.order_id FROM orders AS x", "SELECT order_id FROM o"),
    ("alias-is-model-name", "SELECT customers.order_id FROM orders customers", "SELECT order_id FROM o"),
    ("alias-is-cte-name", "SELECT m_customers.order_id FROM orders AS m_customers", "SELECT order_id FROM o"),
    (
        "in-subquery",
        "SELECT customer_id FROM customers WHERE customer_id IN (SELECT customer_id FROM orders)",
        "SELECT customer_id FROM c WHERE customer_id IN (SELECT customer_id FROM o)",
    ),
    (
        "not-in-subquery",
        "SELECT customer_id FROM customers WHERE customer_id NOT IN (SELECT customer_id FROM orders)",
        "SELECT customer_id FROM c WHERE customer_id NOT IN (SELECT customer_id FROM o)",
    ),
    (
        "any-subquery",
        "SELECT order_id FROM orders WHERE customer_id = ANY (SELECT customer_id FROM customers)",
        "SELECT order_id FROM o WHERE customer_id = ANY (SELECT customer_id FROM c)",
    ),
    (
        "exists-correlated",
        ("SELECT c.customer_id FROM customers c WHERE EXISTS "
        "(SELECT 1 FROM orders o WHERE o.customer_id = c.customer_id)"),
        "SELECT c.customer_id FROM c WHERE EXISTS (SELECT 1 FROM o WHERE o.customer_id = c.customer_id)",
    ),
    (
        "scalar-subquery",
        ("SELECT c.customer_id, (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.customer_id) "
        "FROM customers c"),
        "SELECT c.customer_id, (SELECT COUNT(*) FROM o WHERE o.customer_id = c.customer_id) FROM c",
    ),
    ("derived-table", "SELECT t.order_id FROM (SELECT order_id FROM orders) t", "SELECT order_id FROM o"),
    (
        "user-cte",
        "WITH recent AS (SELECT order_id FROM orders) SELECT order_id FROM recent",
        "SELECT order_id FROM o",
    ),
    (
        "cte-chain",
        ("WITH a AS (SELECT order_id, customer_id FROM orders), b AS (SELECT customer_id FROM a) "
        "SELECT customer_id FROM b"),
        "SELECT customer_id FROM o",
    ),
    (
        "recursive-cte",
        ("WITH RECURSIVE t AS (SELECT order_id FROM orders UNION ALL "
        "SELECT order_id FROM orders WHERE order_id < 0) SELECT order_id FROM t"),
        "SELECT order_id FROM o",
    ),
    ("cte-shadows-model", "WITH orders AS (SELECT 1 AS order_id) SELECT order_id FROM orders", ERR),
    ("cte-shadows-generated", "WITH m_orders AS (SELECT 1 AS order_id) SELECT order_id FROM m_orders", ERR),
    (
        "union",
        "SELECT order_id FROM orders UNION SELECT order_id FROM orders",
        "SELECT order_id FROM o",
    ),
    (
        "union-all-mixed",
        "SELECT customer_id FROM customers UNION ALL SELECT customer_id FROM orders",
        "SELECT customer_id FROM c UNION ALL SELECT customer_id FROM o",
    ),
    (
        "intersect",
        "SELECT customer_id FROM customers INTERSECT SELECT customer_id FROM orders",
        "SELECT customer_id FROM c INTERSECT SELECT customer_id FROM o",
    ),
    (
        "except",
        "SELECT customer_id FROM customers EXCEPT SELECT customer_id FROM orders",
        "SELECT customer_id FROM c EXCEPT SELECT customer_id FROM o",
    ),
    (
        "window-row-number",
        "SELECT order_id, ROW_NUMBER() OVER (ORDER BY order_id) FROM orders",
        "SELECT order_id, ROW_NUMBER() OVER (ORDER BY order_id) FROM o",
    ),
    (
        "window-partition",
        "SELECT order_id, SUM(1) OVER (PARTITION BY region) FROM orders",
        "SELECT order_id, SUM(1) OVER (PARTITION BY region) FROM o",
    ),
    ("metric", "SELECT status, n FROM orders_by_status", "SELECT status, COUNT(*) FROM o GROUP BY status"),
    (
        "left-join",
        ("SELECT c.customer_id, o.order_id FROM customers c LEFT JOIN orders o "
        "ON o.customer_id = c.customer_id"),
        "SELECT c.customer_id, o.order_id FROM c LEFT JOIN o ON o.customer_id = c.customer_id",
    ),
    (
        "right-join",
        ("SELECT c.customer_id, o.order_id FROM orders o RIGHT JOIN customers c "
        "ON o.customer_id = c.customer_id"),
        "SELECT c.customer_id, o.order_id FROM o RIGHT JOIN c ON o.customer_id = c.customer_id",
    ),
    (
        "full-join",
        ("SELECT c.customer_id, o.order_id FROM customers c FULL JOIN orders o "
        "ON o.customer_id = c.customer_id"),
        "SELECT c.customer_id, o.order_id FROM c FULL JOIN o ON o.customer_id = c.customer_id",
    ),
    (
        "lateral",
        ("SELECT c.customer_id, l.cnt FROM customers c JOIN LATERAL "
        "(SELECT COUNT(*) AS cnt FROM orders o WHERE o.customer_id = c.customer_id) l ON TRUE"),
        ("SELECT c.customer_id, l.cnt FROM c JOIN LATERAL "
        "(SELECT COUNT(*) AS cnt FROM o WHERE o.customer_id = c.customer_id) l ON TRUE"),
    ),
    (
        "group-having",
        "SELECT region, COUNT(*) FROM orders GROUP BY region HAVING COUNT(*) > 0",
        "SELECT region, COUNT(*) FROM o GROUP BY region HAVING COUNT(*) > 0",
    ),
    (
        "distinct-order-limit",
        "SELECT DISTINCT status FROM orders ORDER BY status LIMIT 10 OFFSET 0",
        "SELECT DISTINCT status FROM o ORDER BY status",
    ),
    (
        "case-expression",
        "SELECT CASE WHEN region = 'West' THEN 'w' ELSE 'other' END FROM orders",
        "SELECT CASE WHEN region = 'West' THEN 'w' ELSE 'other' END FROM o",
    ),
    (
        "where-other-region",
        "SELECT order_id FROM orders WHERE region = 'South' OR region = 'West'",
        "SELECT order_id FROM o WHERE region = 'South' OR region = 'West'",
    ),
    (
        "star-of-join",
        "SELECT o.* FROM orders o JOIN customers c ON c.customer_id = o.customer_id",
        ("SELECT o.order_id, o.customer_id, o.ordered_at, o.status, o.region "
        "FROM o JOIN c ON c.customer_id = o.customer_id"),
    ),
    ("self-join", "SELECT a.order_id FROM orders a JOIN orders b ON a.order_id = b.order_id", ERR),
    ("cross-join", "SELECT a.order_id FROM orders a CROSS JOIN customers c", ERR),
]

CASES = [(shape, user) for shape in SHAPES for user in USERS]


def test_the_suite_has_at_least_sixty_cases_for_three_users() -> None:
    assert len(CASES) >= 60 and len(USERS) == 3


@pytest.fixture(scope="module")
def warehouse() -> PostgresWarehouseAdapter:
    dsn = os.getenv("WAREHOUSE_DSN")
    if not dsn:
        pytest.skip("WAREHOUSE_DSN is required for the row-level security suite")
    return PostgresWarehouseAdapter(dsn, statement_timeout_ms=5_000, max_rows=1000)


def _normalised(rows: tuple[tuple[Any, ...], ...]) -> list[str]:
    return sorted(str(row) for row in rows)


@pytest.mark.parametrize(
    ("shape", "user"), CASES, ids=[f"{shape[0]}-{user}" for shape, user in CASES]
)
def test_user_sees_exactly_the_permitted_rows(
    shape: tuple[str, str, str], user: str, warehouse: PostgresWarehouseAdapter
) -> None:
    _, logical, oracle = shape
    compiled = compile_query(logical, MDL, USERS[user], POLICIES)
    if oracle == ERR:
        assert not compiled.ok, "this shape must be refused by the compiler"
        return
    assert compiled.ok, compiled.errors
    assert validate_compiled_sql(compiled.sql, MDL).ok
    warehouse.explain(ValidatedQuery(compiled.sql, compiled.parameters))
    actual = warehouse.execute_read_only(
        ValidatedQuery(compiled.sql, compiled.parameters), max_rows=1000
    )
    expected = warehouse.execute_read_only(
        ValidatedQuery((PRE + oracle).replace("%", "%%").replace("%%(r)s", "%(r)s"), {"r": REGION[user]}),
        max_rows=1000,
    )
    assert _normalised(actual.rows) == _normalised(expected.rows)
    if REGION[user] is None:
        assert "WHERE FALSE" in compiled.sql  # missing attribute: fail closed


def test_the_users_really_differ_so_the_oracle_is_meaningful(warehouse: PostgresWarehouseAdapter) -> None:
    def ids(user: str) -> set[Any]:
        compiled = compile_query("SELECT order_id FROM orders", MDL, USERS[user], POLICIES)
        result = warehouse.execute_read_only(ValidatedQuery(compiled.sql, compiled.parameters), max_rows=100)
        return {row[0] for row in result.rows}

    west, south, nobody = ids("west"), ids("south"), ids("no_region")
    assert west and south and not (west & south) and nobody == set()
