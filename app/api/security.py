import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from tempfile import NamedTemporaryFile
from typing import Any

import psycopg
from cryptography.fernet import Fernet, InvalidToken
from psycopg.conninfo import make_conninfo
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.config import Settings


class ConnectionRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    display_name: str = Field(min_length=1, max_length=120, alias="displayName")
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=5432, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr
    database: str = Field(min_length=1, max_length=128)
    ssl: bool = False
    ssl_ca_certificate: str | None = Field(default=None, alias="sslCaCertificate")

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if any(char in value for char in "/?#@"):
            raise ValueError("Host must be a hostname or IP address")
        return value.rstrip(".").lower()

    @field_validator("ssl_ca_certificate")
    @classmethod
    def validate_ca_certificate(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 64_000:
            raise ValueError("CA certificate is too large")
        if value is not None and "BEGIN CERTIFICATE" not in value:
            raise ValueError("CA certificate must be PEM encoded")
        return value


@dataclass(frozen=True)
class ConnectionTestResult:
    server_version: str
    latency_ms: int


class ConnectionTestError(ValueError):
    """Raised when a requested warehouse connection cannot be safely used."""


def get_fernet(settings: Settings) -> Fernet:
    if not settings.connection_encryption_key:
        raise ValueError("CONNECTION_ENCRYPTION_KEY is not configured")
    try:
        return Fernet(settings.connection_encryption_key.encode("ascii"))
    except (ValueError, TypeError) as error:
        raise ValueError("CONNECTION_ENCRYPTION_KEY must be a valid Fernet key") from error


def encrypt_connection(request: ConnectionRequest, settings: Settings) -> bytes:
    payload = {
        "username": request.username,
        "password": request.password.get_secret_value(),
        "database": request.database,
        "ssl_ca_certificate": request.ssl_ca_certificate,
    }
    return get_fernet(settings).encrypt(json.dumps(payload).encode("utf-8"))


def decrypt_connection(encrypted: bytes, settings: Settings) -> dict[str, str | None]:
    try:
        payload: Any = json.loads(get_fernet(settings).decrypt(encrypted))
    except (InvalidToken, json.JSONDecodeError) as error:
        raise ValueError("Stored connection credentials cannot be decrypted") from error
    if not isinstance(payload, dict):
        raise TypeError("Stored connection credentials have an invalid format")
    username = payload.get("username")
    password = payload.get("password")
    database = payload.get("database")
    ca_certificate = payload.get("ssl_ca_certificate")
    if not all(isinstance(value, str) for value in (username, password, database)):
        raise TypeError("Stored connection credentials have an invalid format")
    if ca_certificate is not None and not isinstance(ca_certificate, str):
        raise TypeError("Stored connection credentials have an invalid format")
    return {
        "username": username,
        "password": password,
        "database": database,
        "ssl_ca_certificate": ca_certificate,
    }


_FORBIDDEN_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),  # link-local, includes cloud metadata 169.254.169.254
    ipaddress.ip_network("fe80::/10"),
)
_FORBIDDEN_NAMES = {"metadata.google.internal", "metadata", "instance-data"}


def _address_is_forbidden(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)  # ::ffff:169.254.169.254 is the same host
    if mapped is not None:
        address = mapped
    return any(address in network for network in _FORBIDDEN_NETWORKS)


def host_is_forbidden(host: str) -> bool:
    """Link-local and cloud metadata addresses stay refused whatever the allowlist says.

    A name is also refused when any address it resolves to is forbidden; a name that does not
    resolve here is left to the connection attempt.
    """
    normalized = host.rstrip(".").casefold()
    if normalized in _FORBIDDEN_NAMES:
        return True
    try:
        return _address_is_forbidden(ipaddress.ip_address(normalized))
    except ValueError:
        pass
    try:
        resolved = socket.getaddrinfo(normalized, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return False
    return any(
        _address_is_forbidden(ipaddress.ip_address(str(info[4][0]).split("%")[0]))
        for info in resolved
    )


def validate_allowed_hosts(hosts: list[str]) -> list[str]:
    """Clean an edited allowlist; entries are hostnames, IPs or CIDR ranges, never forbidden."""
    cleaned: list[str] = []
    for raw in hosts:
        item = raw.strip().casefold()
        if not item or len(item) > 253 or any(char in item for char in "?#@ "):
            raise ValueError(f"Invalid host entry {raw!r}")
        if "/" in item:
            try:
                network = ipaddress.ip_network(item, strict=False)
            except ValueError as error:
                raise ValueError(f"Invalid network {raw!r}") from error
            mapped = getattr(network.network_address, "ipv4_mapped", None)
            if mapped is not None and network.prefixlen >= 96:
                network = ipaddress.ip_network(f"{mapped}/{network.prefixlen - 96}", strict=False)
            if any(
                network.version == blocked.version and network.overlaps(blocked)
                for blocked in _FORBIDDEN_NETWORKS
            ):
                raise ValueError(f"{raw!r} includes link-local or metadata addresses")
        elif host_is_forbidden(item):
            raise ValueError(f"{raw!r} is a link-local or metadata address")
        if item not in cleaned:
            cleaned.append(item)
    return cleaned


def _host_is_allowed(host: str, allowlist: tuple[str, ...]) -> bool:
    normalized = host.rstrip(".").casefold()
    for item in allowlist:
        candidate = item.strip().casefold()
        if not candidate:
            continue
        try:
            network = ipaddress.ip_network(candidate, strict=False)
            address = ipaddress.ip_address(normalized)
            if address in network:
                return True
        except ValueError:
            if normalized == candidate:
                return True
    return False


def test_connection(
    request: ConnectionRequest,
    settings: Settings,
) -> ConnectionTestResult:
    if host_is_forbidden(request.host):
        raise ConnectionTestError("Database host is not allowed")
    if not _host_is_allowed(request.host, settings.allowed_database_hosts):
        raise ConnectionTestError("Database host is not in DB_HOST_ALLOWLIST")

    ssl_root_cert: str | None = None
    try:
        if request.ssl_ca_certificate:
            with NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".pem") as cert_file:
                cert_file.write(request.ssl_ca_certificate)
                cert_file.flush()
                ssl_root_cert = cert_file.name
                result = _run_connection_test(request, ssl_root_cert)
        else:
            result = _run_connection_test(request, ssl_root_cert)
    except ConnectionTestError:
        raise
    except psycopg.Error as error:
        if error.sqlstate and error.sqlstate.startswith("28"):
            message = "Database rejected the supplied credentials"
        elif error.sqlstate and error.sqlstate.startswith("08"):
            message = "Could not reach the database host"
        else:
            message = "Database connection test failed"
        raise ConnectionTestError(message) from error
    return result


def _run_connection_test(
    request: ConnectionRequest,
    ssl_root_cert: str | None,
) -> ConnectionTestResult:
    started = time.perf_counter()
    parameters: dict[str, str] = {
        "host": request.host,
        "port": str(request.port),
        "dbname": request.database,
        "user": request.username,
        "password": request.password.get_secret_value(),
        "connect_timeout": "5",
        "sslmode": "verify-full" if ssl_root_cert else ("require" if request.ssl else "prefer"),
    }
    if ssl_root_cert:
        parameters["sslrootcert"] = ssl_root_cert
    conninfo = make_conninfo(**parameters)
    with psycopg.connect(conninfo) as connection, connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(
            """
            SELECT current_setting('server_version') AS server_version,
                (
                    (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)
                    OR EXISTS (
                        SELECT 1 FROM pg_namespace n
                        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                          AND left(n.nspname, 3) <> 'pg_'
                          AND has_schema_privilege(current_user, n.oid, 'CREATE')
                    )
                    OR EXISTS (
                        SELECT 1 FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                          AND left(n.nspname, 3) <> 'pg_'
                          AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                          AND (
                            has_table_privilege(current_user, c.oid, 'INSERT')
                            OR has_table_privilege(current_user, c.oid, 'UPDATE')
                            OR has_table_privilege(current_user, c.oid, 'DELETE')
                            OR has_table_privilege(current_user, c.oid, 'TRUNCATE')
                          )
                    )
                ) AS can_write
            """
        )
        row = cursor.fetchone()
        if row is None:
            raise ConnectionTestError("Database did not return server details")
        server_version = str(row[0])
        can_write = bool(row[1])
    if can_write:
        raise ConnectionTestError("Use a read-only database account; this account can write")
    return ConnectionTestResult(
        server_version=server_version,
        latency_ms=round((time.perf_counter() - started) * 1000),
    )
