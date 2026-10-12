import pytest

from app.adapters.warehouse.postgres_catalog import mask_sample_value
from app.core.catalog import (
    CatalogColumn,
    CatalogForeignKey,
    CatalogTable,
    CatalogTableProfile,
)
from app.domain.mdl import RelationshipSource
from app.semantic.profiling import ProfilingService


class FakeCatalog:
    def __init__(self, profiles: tuple[CatalogTableProfile, ...]) -> None:
        self.profiles = profiles

    def list_tables(self, schemas: tuple[str, ...] = ()) -> tuple[CatalogTable, ...]:
        return tuple(profile.table for profile in self.profiles)

    def profile_tables(
        self,
        tables: tuple[tuple[str, str], ...],
        *,
        sample_limit: int = 3,
    ) -> tuple[CatalogTableProfile, ...]:
        requested = set(tables)
        return tuple(
            profile
            for profile in self.profiles
            if (profile.table.schema_name, profile.table.table_name) in requested
        )


@pytest.fixture
def ecommerce_profiles() -> tuple[CatalogTableProfile, ...]:
    customers = CatalogTable("ecommerce", "customers", "BASE TABLE")
    orders = CatalogTable("ecommerce", "orders", "BASE TABLE")
    return (
        CatalogTableProfile(
            table=customers,
            columns=(
                CatalogColumn("customer_id", "integer", False, True),
                CatalogColumn("email", "text", False, False),
            ),
            foreign_keys=(),
            row_count=2,
            sample_rows=(
                {"customer_id": 1, "email": "[REDACTED]"},
                {"customer_id": 2, "email": "[REDACTED]"},
            ),
        ),
        CatalogTableProfile(
            table=orders,
            columns=(
                CatalogColumn("order_id", "integer", False, True),
                CatalogColumn("customer_id", "integer", False, False),
                CatalogColumn("ordered-at", "timestamp with time zone", False, False),
            ),
            foreign_keys=(
                CatalogForeignKey(
                    "orders_customer_id_fkey",
                    ("customer_id",),
                    "ecommerce",
                    "customers",
                    ("customer_id",),
                ),
            ),
            row_count=5,
            sample_rows=({"order_id": 1, "customer_id": 1, "ordered-at": "2026-10-12"},),
        ),
    )


def test_profile_builds_mdl_and_database_relationships(ecommerce_profiles) -> None:
    service = ProfilingService(FakeCatalog(ecommerce_profiles))

    result = service.create_draft(
        (("ecommerce", "customers"), ("ecommerce", "orders"))
    )

    assert result.summary.selected_tables == 2
    assert result.summary.total_rows == 7
    assert {model.name for model in result.mdl.models} == {"customers", "orders"}
    orders = next(model for model in result.mdl.models if model.name == "orders")
    assert orders.table_reference == "ecommerce.orders"
    assert orders.primary_key == "order_id"
    assert next(column for column in orders.columns if column.physical_name).name == "ordered_at"
    relationship = result.mdl.relationships[0]
    assert relationship.source == RelationshipSource.DATABASE_FOREIGN_KEY
    assert relationship.condition == "orders.customer_id = customers.customer_id"
    assert "customers" in result.summary.models_needing_review
    assert result.tables[0].sample_rows[0]["email"] == "[REDACTED]"


def test_profile_infers_relationships_only_from_matching_primary_key_name() -> None:
    customers = CatalogTableProfile(
        CatalogTable("ecommerce", "customers", "BASE TABLE"),
        (
            CatalogColumn("customer_id", "integer", False, True),
            CatalogColumn("name", "text", False, False),
        ),
        (),
        1,
        (),
    )
    order = CatalogTableProfile(
        CatalogTable("ecommerce", "orders", "BASE TABLE"),
        (
            CatalogColumn("order_id", "integer", False, True),
            CatalogColumn("customer_id", "integer", False, False),
        ),
        (),
        1,
        (),
    )

    result = ProfilingService(FakeCatalog((customers, order))).create_draft(
        (("ecommerce", "customers"), ("ecommerce", "orders"))
    )

    assert len(result.mdl.relationships) == 1
    relationship = result.mdl.relationships[0]
    assert relationship.source == RelationshipSource.INFERRED_COLUMN_NAME
    assert relationship.needs_review


def test_profile_skips_self_referential_foreign_keys() -> None:
    employees = CatalogTableProfile(
        CatalogTable("human_resources", "employees", "BASE TABLE"),
        (
            CatalogColumn("employee_id", "integer", False, True),
            CatalogColumn("manager_id", "integer", True, False),
        ),
        (
            CatalogForeignKey(
                "employees_manager_id_fkey",
                ("manager_id",),
                "human_resources",
                "employees",
                ("employee_id",),
            ),
        ),
        4,
        (),
    )

    result = ProfilingService(FakeCatalog((employees,))).create_draft(
        (("human_resources", "employees"),)
    )

    assert result.mdl.relationships == []


@pytest.mark.parametrize(
    ("column", "value", "expected"),
    [
        ("email", "person@example.test", "[REDACTED]"),
        ("customer_name", "Asha Rao", "[REDACTED]"),
        ("annual_salary", 120_000, "[REDACTED]"),
        ("api_token", "sensitive", "[REDACTED]"),
        ("customer_id", 42, 42),
        ("region", "West", "West"),
        ("email", None, None),
    ],
)
def test_sample_masking(column: str, value: object, expected: object) -> None:
    assert mask_sample_value(column, value) == expected


def test_profile_requires_nonempty_unique_table_selection(ecommerce_profiles) -> None:
    service = ProfilingService(FakeCatalog(ecommerce_profiles))

    with pytest.raises(ValueError, match="at least one"):
        service.create_draft(())
    with pytest.raises(ValueError, match="unique"):
        service.create_draft((("ecommerce", "orders"), ("ecommerce", "orders")))
