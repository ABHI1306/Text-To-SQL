"""Semantic compiler: the v1 compile test set plus targeted safety and shape tests."""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.mdl import Mdl
from app.domain.policy import RowPolicy
from app.semantic.compiler import CompileErrorCode, compile_query
from tests.support.ecommerce_mdl import (
    build_mdl,
    build_policies,
    build_principal,
    build_users,
    load_cases,
)

CASES = load_cases()["cases"]


@pytest.fixture(scope="module")
def mdl() -> Mdl:
    return build_mdl()


@pytest.fixture(scope="module")
def policies() -> list[RowPolicy]:
    return build_policies()


@pytest.fixture(scope="module")
def users() -> dict[str, Any]:
    return build_users()


def test_compile_set_has_forty_cases() -> None:
    assert len(CASES) == 40


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_compile_case(
    case: dict[str, Any], mdl: Mdl, policies: list[RowPolicy], users: dict[str, Any]
) -> None:
    result = compile_query(case["logical_sql"], mdl, users.get(case["user"]), policies)
    expect = case["expect"]
    if expect["status"] == "error":
        assert not result.ok
        assert result.sql == ""
        assert result.errors[0].code.value == expect["error_code"], result.errors
        return
    assert result.ok, result.errors
    assert result.sql == expect["sql"]
    assert dict(result.parameters) == expect["parameters"]
    lowered = result.sql.lower()
    for fragment in expect["must_contain"]:
        assert fragment.lower() in lowered, fragment
    for fragment in expect["must_not_contain"]:
        assert fragment.lower() not in lowered, fragment
    assert set(result.physical_relations) == set(expect["physical_relations"])


def compile_as(sql: str, mdl: Mdl, policies: list[RowPolicy], user: Any = None):
    return compile_query(sql, mdl, user, policies)


def test_errors_carry_identifier_and_closest_names(mdl: Mdl) -> None:
    unknown_model = compile_query("SELECT 1 FROM ordres", mdl, None)
    assert unknown_model.errors[0].identifier == "ordres"
    assert "orders" in unknown_model.errors[0].suggestions
    unknown_column = compile_query("SELECT order_dt FROM orders", mdl, None)
    assert unknown_column.errors[0].code is CompileErrorCode.UNKNOWN_COLUMN
    assert unknown_column.errors[0].identifier == "order_dt"
    assert "ordered_at" in unknown_column.errors[0].suggestions


def test_every_unknown_relation_is_reported(mdl: Mdl) -> None:
    result = compile_query("SELECT 1 FROM ghosts g JOIN phantoms p ON 1 = 1", mdl, None)
    assert {error.identifier for error in result.errors} == {"ghosts", "phantoms"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM information_schema.columns",
        "SELECT * FROM pg_user",
        "SELECT * FROM orders o JOIN pg_catalog.pg_class c ON true",
        "SELECT (SELECT 1 FROM public.orders)",
        "SELECT * FROM orders WHERE order_id IN (SELECT oid FROM pg_class)",
    ],
)
def test_physical_and_catalog_relations_are_rejected_anywhere(sql: str, mdl: Mdl) -> None:
    result = compile_query(sql, mdl, None)
    assert result.errors[0].code is CompileErrorCode.UNKNOWN_MODEL


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_ls_dir('/')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT dblink('host=x', 'select 1')",
        "SELECT current_setting('is_superuser')",
        "SELECT version()",
        "SELECT current_user",
        "SELECT order_id FROM orders WHERE pg_sleep(5) IS NULL",
        "SELECT * FROM generate_series(1, 10)",
    ],
)
def test_disallowed_functions_are_rejected_anywhere(sql: str, mdl: Mdl) -> None:
    assert compile_query(sql, mdl, None).errors[0].code is CompileErrorCode.DISALLOWED_FUNCTION


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO orders VALUES (1)",
        "DROP TABLE orders",
        "TRUNCATE orders",
        "COPY orders TO '/tmp/x'",
        "SELECT * FROM orders FOR UPDATE",
        "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d",
    ],
)
def test_non_select_statements_are_rejected(sql: str, mdl: Mdl) -> None:
    assert compile_query(sql, mdl, None).errors[0].code is CompileErrorCode.NON_SELECT


def test_query_parameters_in_logical_sql_are_rejected(mdl: Mdl) -> None:
    result = compile_query("SELECT order_id FROM orders WHERE region = %(p_region)s", mdl, None)
    assert not result.ok


def test_with_name_may_not_shadow_a_model(mdl: Mdl) -> None:
    sql = "WITH orders AS (SELECT * FROM orders) SELECT order_id FROM orders"
    assert compile_query(sql, mdl, None).errors[0].code is CompileErrorCode.UNKNOWN_MODEL


def test_policy_applies_to_every_use_of_a_model(
    mdl: Mdl, policies: list[RowPolicy], users: dict[str, Any]
) -> None:
    sql = (
        "SELECT o.order_id FROM orders o WHERE o.order_id IN "
        "(SELECT order_id FROM orders UNION ALL SELECT order_id FROM orders) "
        "AND EXISTS (SELECT 1 FROM orders x WHERE x.order_id = o.order_id)"
    )
    result = compile_as(sql, mdl, policies, users["eu_user"])
    assert result.ok, result.errors
    assert result.sql.count("ecommerce.orders") == 1
    assert result.sql.count("%(p_region)s") == 1
    assert "ecommerce." not in result.sql.split(") SELECT", 1)[1]


def test_policy_values_are_bound_never_concatenated(mdl: Mdl, policies: list[RowPolicy]) -> None:
    hostile = build_principal("evil", {"region": "EU' OR 1=1 --"})
    result = compile_as("SELECT order_id FROM orders", mdl, policies, hostile)
    assert "OR 1=1" not in result.sql
    assert result.parameters == {"p_region": "EU' OR 1=1 --"}


def test_missing_attribute_denies_and_is_reported(mdl: Mdl, policies: list[RowPolicy]) -> None:
    result = compile_as("SELECT order_id FROM orders", mdl, policies, build_principal("x", {}))
    assert "WHERE FALSE" in result.sql
    assert result.missing_attributes == ("region",)
    null_attribute = build_principal("y", {"region": None})
    assert "WHERE FALSE" in compile_as("SELECT order_id FROM orders", mdl, policies, null_attribute).sql


def test_dba_is_not_exempt_by_default(mdl: Mdl, policies: list[RowPolicy]) -> None:
    dba = build_principal("dba", {"region": "EU"}, role="dba")
    assert "%(p_region)s" in compile_as("SELECT order_id FROM orders", mdl, policies, dba).sql


def test_no_principal_applies_no_policies(mdl: Mdl, policies: list[RowPolicy]) -> None:
    result = compile_as("SELECT order_id FROM orders", mdl, policies, None)
    assert result.parameters == {}
    assert result.applied_policies == ()


def test_percent_signs_are_escaped_for_parameterised_execution(mdl: Mdl) -> None:
    result = compile_query(
        "SELECT order_id % 2 AS parity FROM orders WHERE status LIKE 'ship%'", mdl, None
    )
    assert "% 2" not in result.sql.replace("%%", "")
    assert "'ship%%'" in result.sql


def test_policy_context_changes_with_attributes_and_policies(
    mdl: Mdl, policies: list[RowPolicy]
) -> None:
    sql = "SELECT order_id FROM orders"
    eu = compile_as(sql, mdl, policies, build_principal("a", {"region": "EU"}))
    eu_again = compile_as(sql, mdl, policies, build_principal("b", {"region": "EU"}))
    us = compile_as(sql, mdl, policies, build_principal("c", {"region": "US"}))
    unrestricted = compile_as(sql, mdl, [], build_principal("d", {"region": "EU"}))
    assert eu.policy_context == eu_again.policy_context
    assert len({eu.policy_context, us.policy_context, unrestricted.policy_context}) == 3


def test_only_used_columns_are_selected_in_a_model_cte(mdl: Mdl) -> None:
    result = compile_query("SELECT status FROM orders", mdl, None)
    assert "SELECT status FROM ecommerce.orders" in result.sql
    assert "customer_id" not in result.sql


def test_select_star_expands_to_model_columns_only(mdl: Mdl) -> None:
    result = compile_query("SELECT * FROM categories", mdl, None)
    assert result.ok
    assert "SELECT category_id, name FROM ecommerce.categories" in result.sql


def test_join_must_match_the_declared_condition(mdl: Mdl) -> None:
    wrong_column = "SELECT 1 FROM orders o JOIN customers c ON c.customer_id = o.order_id"
    assert compile_query(wrong_column, mdl, None).errors[0].code is CompileErrorCode.JOIN_NOT_ALLOWED
    reversed_sides = "SELECT 1 FROM customers c JOIN orders o ON o.customer_id = c.customer_id"
    assert compile_query(reversed_sides, mdl, None).ok


def test_comma_join_between_related_models_gets_its_condition(mdl: Mdl) -> None:
    result = compile_query("SELECT o.order_id FROM orders o, customers c", mdl, None)
    assert result.ok
    assert "JOIN m_customers AS c ON" in result.sql


def test_calculated_fields_expand_with_physical_names() -> None:
    mdl = Mdl.model_validate(
        {
            "models": [
                {
                    "name": "sales",
                    "tableReference": "public.sales_raw",
                    "description": "sales",
                    "columns": [
                        {"name": "amount", "type": "numeric", "physicalName": "amt", "description": "a"},
                        {"name": "fee", "type": "numeric", "physicalName": "fee_amt", "description": "f"},
                        {
                            "name": "net",
                            "type": "numeric",
                            "isCalculated": True,
                            "expression": "amount - fee",
                            "description": "n",
                        },
                        {
                            "name": "net_x2",
                            "type": "numeric",
                            "isCalculated": True,
                            "expression": "net * 2",
                            "description": "n",
                        },
                    ],
                }
            ]
        }
    )
    result = compile_query("SELECT net_x2 FROM sales", mdl, None)
    assert "(amt - fee_amt) * 2 AS net_x2" in result.sql
    renamed = compile_query("SELECT amount FROM sales", mdl, None)
    assert "amt AS amount" in renamed.sql


def test_metric_without_dimensions_aggregates_to_one_row() -> None:
    mdl = build_mdl(
        {
            "metrics": [
                {
                    "name": "total_paid",
                    "base_model": "payments",
                    "dimensions": [],
                    "measures": [{"name": "total", "expression": "sum(amount)"}],
                }
            ]
        }
    )
    result = compile_query("SELECT total FROM total_paid", mdl, None)
    assert result.ok
    assert "GROUP BY" not in result.sql


def test_policy_predicate_errors_are_typed(mdl: Mdl) -> None:
    bad = RowPolicy.model_validate(
        {"name": "bad", "model": "orders", "predicate": "ghost_column = {{ user.region }}"}
    )
    result = compile_query("SELECT order_id FROM orders", mdl, build_principal("u", {"region": "EU"}), [bad])
    assert result.errors[0].code is CompileErrorCode.POLICY_ERROR
    subquery = RowPolicy.model_validate(
        {"name": "sq", "model": "orders", "predicate": "order_id IN (SELECT order_id FROM payments)"}
    )
    result = compile_query("SELECT order_id FROM orders", mdl, build_principal("u", {}), [subquery])
    assert result.errors[0].code is CompileErrorCode.POLICY_ERROR
