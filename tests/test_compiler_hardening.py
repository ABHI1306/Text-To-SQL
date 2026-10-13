"""Regressions for the findings of the adversarial SQL-safety and RLS reviews."""

from __future__ import annotations

import pytest

from app.domain.policy import RowPolicy
from app.semantic.compiler import CompileErrorCode, compile_query
from app.semantic.validator import validate_compiled_sql
from tests.support.ecommerce_mdl import build_mdl, build_policies, build_principal

MDL = build_mdl()
EU = build_principal("eu", {"region": "EU"})


@pytest.mark.parametrize(
    "sql",
    [
        # a CTE named like a catalog view is not visible to its own body ...
        "WITH pg_user AS (SELECT * FROM pg_user) SELECT * FROM pg_user",
        # ... nor to an earlier sibling ...
        "WITH a AS (SELECT * FROM pg_roles), pg_roles AS (SELECT 1 x) SELECT * FROM a",
        # ... nor outside the subquery that owns the WITH
        "SELECT * FROM (WITH pg_class AS (SELECT 1 a) SELECT * FROM pg_class) x, pg_class",
        "WITH orders_copy AS (SELECT 1 x) SELECT * FROM secrets, orders_copy",
    ],
)
def test_a_with_name_cannot_disguise_a_real_relation(sql: str) -> None:
    result = compile_query(sql, MDL, EU, build_policies())
    assert not result.ok and result.errors[0].code is CompileErrorCode.UNKNOWN_MODEL


def test_legitimate_cte_scoping_still_works() -> None:
    sql = (
        "WITH a AS (SELECT order_id FROM orders), b AS (SELECT order_id FROM a) "
        "SELECT order_id FROM b WHERE order_id IN (SELECT order_id FROM a)"
    )
    assert compile_query(sql, MDL, EU, build_policies()).ok
    recursive = (
        "WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM t WHERE n < 3) "
        "SELECT n FROM t"
    )
    assert compile_query(recursive, MDL, EU, build_policies()).ok


@pytest.mark.parametrize(
    "compiled",
    [
        "WITH pg_user AS (SELECT * FROM pg_user) SELECT * FROM pg_user",
        "WITH a AS (SELECT * FROM pg_roles), pg_roles AS (SELECT 1 x) SELECT * FROM a",
        "SELECT * FROM (WITH pg_class AS (SELECT 1 a) SELECT * FROM pg_class) x, pg_class",
    ],
)
def test_the_validator_has_the_same_scoping_rule(compiled: str) -> None:
    assert not validate_compiled_sql(compiled, MDL).ok


@pytest.mark.parametrize("depth", [50, 200, 1000])
def test_deep_nesting_is_a_typed_error_not_a_crash(depth: int) -> None:
    sql = "SELECT " + "(" * depth + "1" + ")" * depth
    result = compile_query(sql, MDL, EU, [])
    assert not result.ok and result.errors[0].code is CompileErrorCode.SYNTAX_ERROR
    validate_compiled_sql(sql, MDL)  # must not raise


def test_oversized_input_is_refused() -> None:
    result = compile_query("SELECT order_id FROM orders WHERE " + " OR ".join(["order_id = 1"] * 5000), MDL, EU, [])
    assert not result.ok and "too long" in result.errors[0].message


def test_a_bare_select_is_a_syntax_error() -> None:
    assert compile_query("SELECT", MDL, EU, []).errors[0].code is CompileErrorCode.SYNTAX_ERROR


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT current_query()",
        "SELECT to_regclass('pg_shadow')",
        "SELECT has_any_column_privilege('orders', 'select')",
        "SELECT inet_server_port()",
        "SELECT obj_description(1)",
        "SELECT cursor_to_xml('c', 1, true, true, '')",
        "SELECT current_catalog",
        "SELECT 'pg_shadow'::regclass",
        "SELECT 'x'::regproc",
        "SELECT generate_series(1, 100000000000)",
        "SELECT order_id FROM orders, LATERAL generate_series(1, 10) g",
    ],
)
def test_functions_outside_the_allowlist_are_refused(sql: str) -> None:
    result = compile_query(sql, MDL, EU, [])
    assert not result.ok and result.errors[0].code is CompileErrorCode.DISALLOWED_FUNCTION


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*), SUM(quantity), AVG(unit_price) FROM order_items",
        "SELECT date_trunc('month', ordered_at), COALESCE(status, 'x') FROM orders",
        "SELECT ROW_NUMBER() OVER (ORDER BY order_id) FROM orders",
        "SELECT UPPER(status), LENGTH(status), ROUND(1.5) FROM orders",
        "SELECT CAST(order_id AS TEXT), order_id::numeric FROM orders",
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY unit_price) FROM order_items",
    ],
)
def test_everyday_analytics_functions_still_compile(sql: str) -> None:
    result = compile_query(sql, MDL, EU, [])
    assert result.ok, result.errors
    assert validate_compiled_sql(result.sql, MDL).ok


def test_policy_roles_match_case_insensitively() -> None:
    policy = RowPolicy.model_validate(
        {"name": "p", "model": "orders", "predicate": "region = {{ user.region }}",
         "appliesTo": {"roles": ["Analyst"]}}
    )
    analyst = build_principal("a", {"region": "EU"}, role="analyst")
    result = compile_query("SELECT order_id FROM orders", MDL, analyst, [policy])
    assert result.applied_policies == ("p",)
    exempt = RowPolicy.model_validate({**policy.model_dump(by_alias=True), "appliesTo": {"exemptRoles": ["ANALYST"]}})
    assert compile_query("SELECT order_id FROM orders", MDL, analyst, [exempt]).applied_policies == ()


def test_attribute_tokens_that_share_a_prefix_do_not_corrupt_the_sql() -> None:
    policy = RowPolicy.model_validate(
        {"name": "p", "model": "orders", "predicate": "region = {{ user.a }} AND status = {{ user.a__b }}"}
    )
    user = build_principal("u", {"a": "EU", "a__b": "paid"})
    result = compile_query("SELECT order_id FROM orders", MDL, user, [policy])
    assert result.ok and "%(p_a)s" in result.sql and "%(p_a__b)s" in result.sql
    assert "__" not in result.sql.replace("p_a__b", "")
    assert dict(result.parameters) == {"p_a": "EU", "p_a__b": "paid"}


def test_policy_predicates_obey_the_function_rules_too() -> None:
    policy = RowPolicy.model_validate(
        {"name": "p", "model": "orders", "predicate": "region = {{ user.region }} AND pg_sleep(1) IS NULL"}
    )
    result = compile_query("SELECT order_id FROM orders", MDL, EU, [policy])
    assert result.errors[0].code is CompileErrorCode.POLICY_ERROR
