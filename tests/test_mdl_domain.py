import pytest
from pydantic import ValidationError

from app.domain.mdl import (
    Mdl,
    MdlValidationError,
    validate_mdl_against_catalog,
)


def valid_mdl() -> dict[str, object]:
    return {
        "version": 3,
        "dialect": "postgresql",
        "models": [
            {
                "name": "orders",
                "tableReference": "ecommerce.orders",
                "description": "One row per order",
                "primaryKey": "order_id",
                "columns": [
                    {
                        "name": "order_id",
                        "type": "integer",
                        "description": "Unique order identifier",
                        "isPrimaryKey": True,
                    },
                    {"name": "customer_id", "type": "integer", "description": "Customer"},
                    {
                        "name": "net_revenue",
                        "type": "numeric",
                        "description": "Revenue after refunds",
                        "isCalculated": True,
                        "expression": "amount - refunds",
                    },
                    {"name": "amount", "type": "numeric", "description": "Gross amount"},
                    {"name": "refunds", "type": "numeric", "description": "Refund amount"},
                ],
            },
            {
                "name": "customers",
                "tableReference": "ecommerce.customers",
                "description": "One row per customer",
                "primaryKey": "customer_id",
                "columns": [
                    {
                        "name": "customer_id",
                        "type": "integer",
                        "description": "Unique customer identifier",
                        "isPrimaryKey": True,
                    },
                    {"name": "region", "type": "text", "description": "Sales region"},
                ],
            },
        ],
        "relationships": [
            {
                "name": "orders_customers",
                "models": ["orders", "customers"],
                "joinType": "MANY_TO_ONE",
                "condition": "orders.customer_id = customers.customer_id",
            }
        ],
        "metrics": [
            {
                "name": "monthly_revenue",
                "baseObject": "orders",
                "dimensions": [{"name": "order_month", "expression": "date_trunc('month', order_id)"}],
                "measures": [{"name": "revenue", "expression": "sum(net_revenue)"}],
            }
        ],
        "instructions": ["Fiscal year starts on 1 April"],
        "layout": {"orders": {"x": 120, "y": 80}},
    }


def test_valid_mdl_parses_with_camel_case_json_aliases() -> None:
    mdl = Mdl.model_validate(valid_mdl())

    assert mdl.models[0].table_reference == "ecommerce.orders"
    assert mdl.models[0].columns[0].is_primary_key
    assert mdl.metrics[0].base_object == "orders"
    assert mdl.model_dump(by_alias=True)["models"][0]["tableReference"] == "ecommerce.orders"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data["models"].append(data["models"][0].copy()), "duplicate model"),
        (lambda data: data["models"][0].update(name="Orders"), "must match"),
        (lambda data: data["models"][0].update(primaryKey="missing"), "primaryKey"),
        (
            lambda data: data["models"][0]["columns"][2].update(expression="amount - absent"),
            "unknown column",
        ),
        (lambda data: data["relationships"][0].update(models=["orders", "absent"]), "unknown model"),
        (
            lambda data: data["relationships"][0].update(
                condition="orders.unknown_id = customers.customer_id"
            ),
            "unknown or unqualified reference",
        ),
        (lambda data: data["metrics"][0].update(baseObject="absent"), "unknown baseObject"),
        (
            lambda data: data["metrics"][0]["measures"][0].update(expression="sum(absent)"),
            "unknown column",
        ),
    ],
)
def test_invalid_mdl_rules_are_rejected(mutate, message: str) -> None:
    document = valid_mdl()
    mutate(document)

    with pytest.raises(ValidationError, match=message):
        Mdl.model_validate(document)


def test_invalid_postgres_expression_is_rejected() -> None:
    document = valid_mdl()
    document["models"][0]["columns"][2]["expression"] = "amount +"

    with pytest.raises(ValidationError, match="invalid PostgreSQL expression"):
        Mdl.model_validate(document)


def test_duplicate_column_names_are_rejected() -> None:
    document = valid_mdl()
    document["models"][0]["columns"].append(document["models"][0]["columns"][0].copy())

    with pytest.raises(ValidationError, match="duplicate column"):
        Mdl.model_validate(document)


def test_blank_descriptions_are_flagged_for_review() -> None:
    document = valid_mdl()
    document["models"][0]["description"] = ""
    document["models"][0]["columns"][0]["description"] = ""

    mdl = Mdl.model_validate(document)

    assert mdl.models[0].needs_review
    assert mdl.models[0].columns[0].needs_review


def test_large_table_with_undescribed_column_is_flagged_for_review() -> None:
    document = valid_mdl()
    document["models"][0]["approxRows"] = 100_000
    document["models"][0]["columns"][0]["description"] = ""

    assert Mdl.model_validate(document).models[0].needs_review


def test_catalog_validation_rejects_missing_table_column_and_incompatible_type() -> None:
    mdl = Mdl.model_validate(valid_mdl())

    with pytest.raises(MdlValidationError, match="does not exist"):
        validate_mdl_against_catalog(mdl, {})
    with pytest.raises(MdlValidationError, match="column does not exist"):
        validate_mdl_against_catalog(
            mdl,
            {
                "ecommerce.orders": {"id": "integer"},
                "ecommerce.customers": {"customer_id": "integer", "region": "text"},
            },
        )
    with pytest.raises(MdlValidationError, match="incompatible"):
        validate_mdl_against_catalog(
            mdl,
            {
                "ecommerce.orders": {
                    "order_id": "text",
                    "customer_id": "integer",
                    "amount": "numeric",
                    "refunds": "numeric",
                },
                "ecommerce.customers": {"customer_id": "integer", "region": "text"},
            },
        )


def test_catalog_validation_uses_physical_column_name_when_logical_name_differs() -> None:
    document = valid_mdl()
    document["models"][0]["columns"][0]["physicalName"] = "order-id"
    document["models"][0]["columns"][0]["name"] = "order_id"
    document["models"][0]["columns"][0]["isPrimaryKey"] = True
    mdl = Mdl.model_validate(document)

    validate_mdl_against_catalog(
        mdl,
        {
            "ecommerce.orders": {
                "order-id": "integer",
                "customer_id": "integer",
                "amount": "numeric",
                "refunds": "numeric",
            },
            "ecommerce.customers": {"customer_id": "integer", "region": "text"},
        },
    )
