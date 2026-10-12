import json
import os
import uuid

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict
from psycopg.errors import InsufficientPrivilege, QueryCanceled

from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.adapters.warehouse.postgres import PostgresWarehouseAdapter
from app.adapters.warehouse.postgres_catalog import PostgresCatalogAdapter
from app.api.dependencies import get_auth_service, get_repository
from app.auth.service import AuthenticationError, AuthService, PasswordChangeRequired, hash_password
from app.core.types import ValidatedQuery
from app.main import app
from app.semantic.profiling import ProfilingService


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


def test_catalog_adapter_profiles_selected_tables_and_masks_samples() -> None:
    catalog = PostgresCatalogAdapter(
        _required_dsn("WAREHOUSE_DSN"),
        statement_timeout_ms=2_000,
    )

    tables = catalog.list_tables(("ecommerce",))
    assert ("ecommerce", "orders") in {
        (table.schema_name, table.table_name) for table in tables
    }
    profiles = catalog.profile_tables(
        (("ecommerce", "orders"), ("ecommerce", "customers"))
    )

    orders = next(profile for profile in profiles if profile.table.table_name == "orders")
    customers = next(
        profile for profile in profiles if profile.table.table_name == "customers"
    )
    assert orders.row_count == 4
    assert customers.row_count == 3
    assert orders.columns[0].is_primary_key
    assert any(
        foreign_key.referenced_table == "customers"
        for foreign_key in orders.foreign_keys
    )
    assert customers.sample_rows
    assert customers.sample_rows[0]["email"] == "[REDACTED]"


def test_profiling_service_drafts_mdl_from_seeded_database() -> None:
    service = ProfilingService(
        PostgresCatalogAdapter(
            _required_dsn("WAREHOUSE_DSN"),
            statement_timeout_ms=2_000,
        )
    )

    draft = service.create_draft(
        (("ecommerce", "orders"), ("ecommerce", "customers"))
    )

    assert draft.summary.selected_tables == 2
    assert draft.summary.total_rows == 7
    assert {model.name for model in draft.mdl.models} == {"orders", "customers"}
    assert any(
        relationship.condition == "orders.customer_id = customers.customer_id"
        for relationship in draft.mdl.relationships
    )
    assert all(model.needs_review for model in draft.mdl.models)


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


def test_day2_metadata_authentication_and_project_lifecycle() -> None:
    dsn = _required_dsn("METADATA_APP_DSN")
    repository = PostgresMetadataRepository(dsn)
    username = f"day2-test-{uuid.uuid4().hex}"
    project_id = str(uuid.uuid4())
    user_id = repository.bootstrap_dba(username, hash_password("temporary-test-password"))
    try:
        service = AuthService(
            repository,
            jwt_secret="integration-test-jwt-secret-with-32-bytes",
        )
        login = service.login(username, "temporary-test-password", "127.0.0.1")
        assert login.user.must_change_password
        with pytest.raises(PasswordChangeRequired):
            service.authenticate_access_token(login.access_token)

        repository.create_project(
            project_id=project_id,
            name="Day 2 integration project",
            host="demo-db",
            port=5432,
            database_name="demo",
            ssl_enabled=False,
            encrypted_connection=b"unit-test-encrypted-credentials",
            owner_id=user_id,
        )
        repository.save_selected_tables(
            project_id, [{"schema": "ecommerce", "table": "orders"}]
        )
        document = {
            "version": 3,
            "models": [],
            "relationships": [],
            "metrics": [],
            "instructions": [],
            "layout": {},
        }
        assert repository.save_mdl(project_id, document) == 1
        stored = repository.get_mdl(project_id)
        assert stored is not None
        assert stored["project_id"] == project_id
        assert stored["revision"] == 1
        assert repository.parse_project_table_selection(project_id) == [
            {"schema": "ecommerce", "table": "orders"}
        ]
        assert project_id in repository.get_auth_context(user_id).project_ids

        refreshed = service.refresh(login.refresh_token)
        with pytest.raises(AuthenticationError):
            service.refresh(login.refresh_token)
        with pytest.raises(AuthenticationError):
            service.refresh(refreshed.refresh_token)
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute("DELETE FROM projects WHERE id = %s::uuid", (project_id,))
            connection.execute("DELETE FROM users WHERE id = %s::uuid", (user_id,))


def test_day2_authenticated_setup_api_end_to_end(monkeypatch) -> None:
    warehouse_dsn = _required_dsn("WAREHOUSE_DSN")
    credentials = conninfo_to_dict(warehouse_dsn)
    metadata_dsn = _required_dsn("METADATA_APP_DSN")
    repository = PostgresMetadataRepository(metadata_dsn)
    username = f"day2-api-{uuid.uuid4().hex}"
    user_id = repository.bootstrap_dba(username, hash_password("temporary-api-password"))
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("JWT_SECRET", "integration-test-jwt-secret-with-32-bytes")
    monkeypatch.setenv("CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DB_HOST_ALLOWLIST", "demo-db")
    service = AuthService(
        repository,
        jwt_secret="integration-test-jwt-secret-with-32-bytes",
    )
    app.dependency_overrides[get_auth_service] = lambda: service
    app.dependency_overrides[get_repository] = lambda: repository
    project_id: str | None = None

    class TestLLM:
        def generate(self, prompt: str, *, temperature: float = 0.0) -> str:
            del temperature
            requested = json.loads(prompt.rsplit("\n", 1)[-1])
            return json.dumps(
                {
                    "models": [
                        {
                            "name": model["model"],
                            "description": "Reviewed by the integration test.",
                            "columns": [
                                {
                                    "name": column["name"],
                                    "description": "Reviewed test column.",
                                }
                                for column in model["columns"]
                            ],
                        }
                        for model in requested
                    ]
                }
            )

    monkeypatch.setattr(
        "app.api.routes._profile_service",
        lambda catalog: ProfilingService(catalog, TestLLM()),
    )
    connection = {
        "displayName": "Day 2 live integration",
        "host": credentials["host"],
        "port": int(credentials.get("port", "5432")),
        "username": credentials["user"],
        "password": credentials["password"],
        "database": credentials["dbname"],
        "ssl": False,
    }

    try:
        with TestClient(app) as client:
            login = client.post(
                "/api/v1/auth/login",
                json={"username": username, "password": "temporary-api-password"},
            )
            assert login.status_code == 200
            token = login.json()["access_token"]
            headers = {"authorization": f"Bearer {token}"}
            changed = client.post(
                "/api/v1/auth/change-password",
                headers=headers,
                json={
                    "currentPassword": "temporary-api-password",
                    "newPassword": "changed-api-password",
                },
            )
            assert changed.status_code == 204

            tested = client.post(
                "/api/v1/connections/test",
                headers=headers,
                json=connection,
            )
            assert tested.status_code == 200

            created = client.post(
                "/api/v1/projects",
                headers=headers,
                json=connection,
            )
            assert created.status_code == 201
            project_id = created.json()["id"]

            tables = client.get(
                f"/api/v1/projects/{project_id}/catalog/tables",
                headers=headers,
                params={"schema": "ecommerce", "search": "orders"},
            )
            assert tables.status_code == 200
            assert [item["table"] for item in tables.json()["items"]] == ["orders"]

            selection = client.put(
                f"/api/v1/projects/{project_id}/selected-tables",
                headers=headers,
                json={
                    "tables": [
                        {"schema": "ecommerce", "table": "orders"},
                        {"schema": "ecommerce", "table": "customers"},
                    ]
                },
            )
            assert selection.status_code == 200

            draft = client.post(
                f"/api/v1/projects/{project_id}/introspect",
                headers=headers,
            )
            assert draft.status_code == 200
            assert len(draft.json()["mdl"]["models"]) == 2
            saved = client.put(
                f"/api/v1/projects/{project_id}/mdl",
                headers=headers,
                json=draft.json()["mdl"],
            )
            assert saved.status_code == 200
            assert saved.json()["revision"] == 1

            stored = client.get(
                f"/api/v1/projects/{project_id}/mdl",
                headers=headers,
            )
            assert stored.status_code == 200
            assert stored.json()["revision"] == 1
            setup = client.get(
                f"/api/v1/projects/{project_id}/setup",
                headers=headers,
            )
            assert setup.json()["step"] == "complete"
    finally:
        app.dependency_overrides.clear()
        with psycopg.connect(metadata_dsn, autocommit=True) as metadata:
            if project_id is not None:
                metadata.execute("DELETE FROM projects WHERE id = %s::uuid", (project_id,))
            metadata.execute("DELETE FROM users WHERE id = %s::uuid", (user_id,))
