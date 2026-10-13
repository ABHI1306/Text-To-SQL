"""Validator on compiled SQL: accepts compiler output, rejects anything the compiler never emits."""

from __future__ import annotations

import pytest

from app.semantic.compiler import CompileErrorCode, compile_query
from app.semantic.validator import validate_compiled_sql
from tests.support.ecommerce_mdl import build_mdl, build_policies, build_users, load_cases

MDL = build_mdl()
OK_CASES = [c for c in load_cases()["cases"] if c["expect"]["status"] == "ok"]


@pytest.mark.parametrize("case", OK_CASES, ids=[c["id"] for c in OK_CASES])
def test_every_compiler_output_validates(case: dict) -> None:
    compiled = compile_query(case["logical_sql"], MDL, build_users().get(case["user"]), build_policies())
    assert validate_compiled_sql(compiled.sql, MDL).ok


GOOD = "WITH m_orders AS (SELECT order_id FROM ecommerce.orders) SELECT order_id FROM m_orders AS orders"


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT order_id FROM ecommerce.orders", CompileErrorCode.UNKNOWN_MODEL),
        ("SELECT * FROM orders", CompileErrorCode.UNKNOWN_MODEL),
        ("SELECT usename FROM pg_catalog.pg_user", CompileErrorCode.UNKNOWN_MODEL),
        (GOOD + "; DROP TABLE ecommerce.orders", CompileErrorCode.NON_SELECT),
        ("DELETE FROM ecommerce.orders", CompileErrorCode.NON_SELECT),
        ("WITH d AS (DELETE FROM ecommerce.orders RETURNING *) SELECT * FROM d", CompileErrorCode.NON_SELECT),
        ("SELECT pg_read_file('/etc/passwd')", CompileErrorCode.DISALLOWED_FUNCTION),
        ("SELECT * FROM generate_series(1, 3)", CompileErrorCode.DISALLOWED_FUNCTION),
        ("SELEC 1", CompileErrorCode.SYNTAX_ERROR),
        # a model CTE that reaches for another table, or a different one than its own
        ("WITH m_orders AS (SELECT * FROM ecommerce.customers) SELECT * FROM m_orders", CompileErrorCode.UNKNOWN_MODEL),
        (
            "WITH m_orders AS (SELECT * FROM ecommerce.orders o JOIN ecommerce.customers c ON true) SELECT 1 FROM m_orders",
            CompileErrorCode.UNKNOWN_MODEL,
        ),
        # a physical table smuggled in next to a legitimate model CTE
        (GOOD + " WHERE order_id IN (SELECT customer_id FROM ecommerce.customers)", CompileErrorCode.UNKNOWN_MODEL),
        # policy bypass: a model CTE that is not the compiler's single-table form
        ("WITH m_orders AS (SELECT order_id FROM ecommerce.orders UNION SELECT order_id FROM ecommerce.orders) SELECT 1 FROM m_orders", CompileErrorCode.UNKNOWN_MODEL),
    ],
)
def test_validator_rejects_what_the_compiler_never_emits(sql: str, code: CompileErrorCode) -> None:
    result = validate_compiled_sql(sql, MDL)
    assert not result.ok
    assert result.errors[0].code is code, result.errors


def test_good_sql_passes_and_literal_percent_is_understood() -> None:
    assert validate_compiled_sql(GOOD, MDL).ok
    assert validate_compiled_sql(GOOD + " WHERE order_id %% 2 = 0 AND 'a' LIKE 'a%%'", MDL).ok
