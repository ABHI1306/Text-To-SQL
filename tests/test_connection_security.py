import json

import pytest
from cryptography.fernet import Fernet

from app.api.security import (
    ConnectionRequest,
    ConnectionTestError,
    decrypt_connection,
    encrypt_connection,
)
from app.api.security import (
    test_connection as verify_connection,
)
from app.config import Settings, validate_runtime_secrets


def _settings(
    *,
    app_env: str = "development",
    encryption_key: str = "",
    jwt_secret: str = "",
    allowed_hosts: tuple[str, ...] = ("demo-db", "127.0.0.1/32"),
) -> Settings:
    return Settings(
        app_env=app_env,
        log_level="INFO",
        ollama_url="http://127.0.0.1:11434",
        qdrant_url="http://127.0.0.1:6333",
        warehouse_dsn="",
        warehouse_statement_timeout_ms=10_000,
        warehouse_max_rows=1000,
        metadata_database_url="",
        metadata_migration_url="",
        jwt_secret=jwt_secret,
        connection_encryption_key=encryption_key,
        bootstrap_dba_username="",
        bootstrap_dba_password="",
        allowed_database_hosts=allowed_hosts,
        access_token_ttl_seconds=900,
        refresh_token_ttl_seconds=28_800,
        login_max_failures=5,
        login_lockout_seconds=900,
        sql_model="llama3:latest",
    )


def _request(host: str = "demo-db") -> ConnectionRequest:
    return ConnectionRequest(
        displayName="Demo",
        host=host,
        username="reader",
        password="a-local-test-password",
        database="demo",
    )


def test_connection_credentials_are_encrypted_and_can_be_decrypted() -> None:
    settings = _settings(encryption_key=Fernet.generate_key().decode())
    request = _request()
    encrypted = encrypt_connection(request, settings)
    restored = decrypt_connection(encrypted, settings)
    assert request.password.get_secret_value() not in encrypted.decode("ascii", errors="ignore")
    assert restored == {
        "username": "reader",
        "password": "a-local-test-password",
        "database": "demo",
        "ssl_ca_certificate": None,
    }


def test_decryption_rejects_invalid_credential_shapes() -> None:
    key = Fernet.generate_key().decode()
    settings = _settings(encryption_key=key)
    invalid = Fernet(key.encode()).encrypt(json.dumps({"username": "reader"}).encode())
    with pytest.raises(TypeError, match="invalid format"):
        decrypt_connection(invalid, settings)


def test_connection_test_rejects_hosts_outside_allowlist_before_network_access() -> None:
    with pytest.raises(ConnectionTestError, match="DB_HOST_ALLOWLIST"):
        verify_connection(_request("not-allowed.example"), _settings())


def test_non_development_requires_strong_jwt_and_valid_fernet_key() -> None:
    with pytest.raises(ValueError, match="JWT_SECRET"):
        validate_runtime_secrets(_settings(app_env="production"))
    with pytest.raises(ValueError, match="CONNECTION_ENCRYPTION_KEY"):
        validate_runtime_secrets(
            _settings(
                app_env="production",
                jwt_secret="a-jwt-secret-that-is-long-enough-for-production",
            )
        )
    validate_runtime_secrets(
        _settings(
            app_env="production",
            jwt_secret="a-jwt-secret-that-is-long-enough-for-production",
            encryption_key=Fernet.generate_key().decode(),
        )
    )
