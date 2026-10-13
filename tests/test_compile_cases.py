"""Consistency checks for the compile test set (v1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlglot import exp, parse
from sqlglot.errors import ParseError

DATA = Path(__file__).parent / "data/compile_cases.json"
ERROR_CODES = {
    "UNKNOWN_MODEL",
    "UNKNOWN_COLUMN",
    "JOIN_NOT_ALLOWED",
    "NON_SELECT",
    "DISALLOWED_FUNCTION",
    "SYNTAX_ERROR",
}
CATEGORIES_REQUIRED = {
    "calculated_field",
    "metric",
    "join",
    "alias",
    "row_policy",
    "row_policy_missing_attribute",
    "default_limit",
}


@pytest.fixture(scope="module")
def doc() -> dict[str, Any]:
    return json.loads(DATA.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def models() -> dict[str, set[str]]:
    """Logical models of the deployed E-commerce sample (columns by model)."""
    base = {
        "customers": "customer_id first_name last_name email region created_at",
        "addresses": "address_id customer_id city region country",
        "categories": "category_id name",
        "products": "product_id category_id name unit_price active",
        "orders": "order_id customer_id ordered_at status region",
        "order_items": "order_item_id order_id product_id quantity unit_price",
        "payments": "payment_id order_id paid_at amount method status",
        "inventory": "product_id quantity_on_hand warehouse_region",
    }
    return {name: set(columns.split()) for name, columns in base.items()}


def test_file_is_marked_as_draft_awaiting_human_review(doc: dict[str, Any]) -> None:
    assert doc["version"] == "v1"
    assert doc["status"] == "draft_pending_human_review"


def test_case_ids_are_unique_and_cover_every_required_category(doc: dict[str, Any]) -> None:
    ids = [case["id"] for case in doc["cases"]]
    assert len(ids) == len(set(ids))
    assert CATEGORIES_REQUIRED <= {case["category"] for case in doc["cases"]}


def test_error_cases_use_the_documented_error_codes(doc: dict[str, Any]) -> None:
    codes = {
        case["expect"]["error_code"]
        for case in doc["cases"]
        if case["expect"]["status"] == "error"
    }
    assert codes == ERROR_CODES


def test_overlay_references_known_models(
    doc: dict[str, Any], models: dict[str, set[str]]
) -> None:
    overlay = doc["model_overlay"]
    for field in overlay["calculated_fields"]:
        assert field["model"] in models
    for metric in overlay["metrics"]:
        assert metric["base_model"] in models
    for policy in overlay["row_policies"]:
        assert policy["model"] in models
        assert policy["predicate"].strip()
    for name in {c["user"] for c in doc["cases"] if c["user"]}:
        assert name in overlay["users"]


def _statements(sql: str) -> list[exp.Expression]:
    return [statement for statement in parse(sql, read="postgres") if statement]


def test_logical_sql_parses_except_the_syntax_error_case(doc: dict[str, Any]) -> None:
    for case in doc["cases"]:
        if case["expect"].get("error_code") == "SYNTAX_ERROR":
            with pytest.raises(ParseError):
                _statements(case["logical_sql"])
        else:
            assert _statements(case["logical_sql"]), case["id"]


def test_ok_cases_use_only_models_and_known_columns(
    doc: dict[str, Any], models: dict[str, set[str]]
) -> None:
    overlay = doc["model_overlay"]
    metrics = {m["name"]: m for m in overlay["metrics"]}
    calc: dict[str, set[str]] = {}
    for field in overlay["calculated_fields"]:
        calc.setdefault(field["model"], set()).add(field["name"])
    for case in doc["cases"]:
        if case["expect"]["status"] != "ok":
            continue
        (statement,) = _statements(case["logical_sql"])
        ctes = {c.alias for c in statement.find_all(exp.CTE)}
        tables = {t.name for t in statement.find_all(exp.Table)} - ctes
        assert tables, case["id"]
        assert tables <= set(models) | set(metrics), (case["id"], tables)
        allowed: set[str] = set()
        for table in tables:
            if table in metrics:
                metric = metrics[table]
                allowed |= {d["name"] for d in metric["dimensions"]}
                allowed |= {m["name"] for m in metric["measures"]}
            else:
                allowed |= models[table] | calc.get(table, set())
        aliases = {a.alias for a in statement.find_all(exp.Alias)} | ctes
        for column in statement.find_all(exp.Column):
            assert column.name in allowed | aliases, (case["id"], column.name)


def test_expected_physical_relations_belong_to_the_ecommerce_schema(
    doc: dict[str, Any], models: dict[str, set[str]]
) -> None:
    physical = {f"ecommerce.{name}" for name in models}
    for case in doc["cases"]:
        if case["expect"]["status"] == "ok":
            assert set(case["expect"]["physical_relations"]) <= physical, case["id"]


def test_reject_cases_would_actually_be_rejected_for_the_stated_reason(
    doc: dict[str, Any], models: dict[str, set[str]]
) -> None:
    for case in doc["cases"]:
        expect = case["expect"]
        if expect["status"] != "error" or expect["error_code"] == "SYNTAX_ERROR":
            continue
        statements = _statements(case["logical_sql"])
        if expect["error_code"] == "NON_SELECT":
            assert (
                len(statements) > 1
                or not isinstance(statements[0], exp.Select)
                or statements[0].args.get("into") is not None
            )
        elif expect["error_code"] == "UNKNOWN_MODEL":
            tables = {
                (t.db + "." if t.db else "") + t.name
                for t in statements[0].find_all(exp.Table)
            }
            assert not tables <= set(models), case["id"]
        elif expect["error_code"] == "UNKNOWN_COLUMN":
            columns = {c.name for c in statements[0].find_all(exp.Column)}
            assert not columns <= models["orders"], case["id"]
