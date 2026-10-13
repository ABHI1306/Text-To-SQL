"""The deployed E-commerce sample as an MDL, shared by compiler and policy tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.types import Principal
from app.domain.mdl import Mdl
from app.domain.policy import RowPolicy

COMPILE_CASES = Path(__file__).parent.parent / "data/compile_cases.json"

_TABLES: dict[str, tuple[str, str]] = {
    "customers": (
        ("customer_id:integer first_name:text last_name:text email:text region:text "
         "created_at:timestamptz"),
        "customer_id",
    ),
    "addresses": (
        "address_id:integer customer_id:integer city:text region:text country:text",
        "address_id",
    ),
    "categories": ("category_id:integer name:text", "category_id"),
    "products": (
        "product_id:integer category_id:integer name:text unit_price:numeric active:boolean",
        "product_id",
    ),
    "orders": (
        "order_id:integer customer_id:integer ordered_at:timestamptz status:text region:text",
        "order_id",
    ),
    "order_items": (
        ("order_item_id:integer order_id:integer product_id:integer quantity:integer "
         "unit_price:numeric"),
        "order_item_id",
    ),
    "payments": (
        ("payment_id:integer order_id:integer paid_at:timestamptz amount:numeric method:text "
         "status:text"),
        "payment_id",
    ),
    "inventory": (
        "product_id:integer quantity_on_hand:integer warehouse_region:text",
        "product_id",
    ),
}

_RELATIONSHIPS = (
    ("addresses_customers", "addresses", "customers", "customer_id", "customer_id"),
    ("products_categories", "products", "categories", "category_id", "category_id"),
    ("orders_customers", "orders", "customers", "customer_id", "customer_id"),
    ("order_items_orders", "order_items", "orders", "order_id", "order_id"),
    ("order_items_products", "order_items", "products", "product_id", "product_id"),
    ("payments_orders", "payments", "orders", "order_id", "order_id"),
    ("inventory_products", "inventory", "products", "product_id", "product_id"),
)


def load_cases() -> dict[str, Any]:
    return json.loads(COMPILE_CASES.read_text(encoding="utf-8"))


def build_mdl(overlay: dict[str, Any] | None = None) -> Mdl:
    overlay = overlay or load_cases()["model_overlay"]
    calculated: dict[str, list[dict[str, Any]]] = {}
    for field in overlay.get("calculated_fields", []):
        calculated.setdefault(field["model"], []).append(field)
    models = []
    for name, (columns, key) in _TABLES.items():
        column_defs: list[dict[str, Any]] = []
        for spec in columns.split():
            column_name, column_type = spec.split(":")
            column_defs.append(
                {
                    "name": column_name,
                    "type": column_type,
                    "description": column_name.replace("_", " "),
                    "isPrimaryKey": column_name == key,
                }
            )
        for field in calculated.get(name, []):
            column_defs.append(
                {
                    "name": field["name"],
                    "type": "numeric",
                    "isCalculated": True,
                    "expression": field["expression"],
                    "description": field["name"],
                }
            )
        models.append(
            {
                "name": name,
                "tableReference": f"ecommerce.{name}",
                "description": name,
                "primaryKey": key,
                "columns": column_defs,
            }
        )
    relationships = [
        {
            "name": rel_name,
            "models": [left, right],
            "joinType": "MANY_TO_ONE",
            "condition": f"{left}.{left_column} = {right}.{right_column}",
        }
        for rel_name, left, right, left_column, right_column in _RELATIONSHIPS
    ]
    metrics = [
        {
            "name": metric["name"],
            "baseObject": metric["base_model"],
            "dimensions": metric["dimensions"],
            "measures": metric["measures"],
        }
        for metric in overlay.get("metrics", [])
    ]
    return Mdl.model_validate(
        {"models": models, "relationships": relationships, "metrics": metrics}
    )


def build_policies(overlay: dict[str, Any] | None = None) -> list[RowPolicy]:
    overlay = overlay or load_cases()["model_overlay"]
    return [RowPolicy.model_validate(policy) for policy in overlay.get("row_policies", [])]


def build_principal(name: str, attributes: dict[str, Any], role: str = "user") -> Principal:
    return Principal(
        user_id=name,
        role_name=role,
        capabilities=frozenset(),
        project_ids=frozenset(),
        attributes=attributes,
    )


def build_users(overlay: dict[str, Any] | None = None) -> dict[str, Principal]:
    overlay = overlay or load_cases()["model_overlay"]
    roles = overlay.get("user_roles", {})
    return {
        name: build_principal(name, attributes, roles.get(name, "user"))
        for name, attributes in overlay["users"].items()
    }
