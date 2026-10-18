"""Plain data types for authenticated users, their request context and login results."""

from dataclasses import dataclass
from datetime import datetime

from app.core.types import JsonValue, Principal


@dataclass(frozen=True)
class AuthUser:
    """A stored account as the login path sees it, including lock state and password hash."""
    user_id: str
    username: str
    role_name: str
    password_hash: str | None
    status: str
    must_change_password: bool
    locked_until: datetime | None
    auth_source: str = "local"


@dataclass(frozen=True)
class AuthContext:
    """The authenticated caller on one request: role, capabilities and project access."""
    user_id: str
    username: str
    role_name: str
    capabilities: frozenset[str]
    project_ids: frozenset[str]
    must_change_password: bool
    session_id: str | None = None
    attributes: dict[str, JsonValue] | None = None


@dataclass(frozen=True)
class LoginResult:
    """Tokens and caller context returned by a successful login or refresh."""
    access_token: str
    refresh_token: str
    user: AuthContext
    expires_in: int


def to_principal(context: AuthContext) -> Principal:
    """The identity the compiler sees: the account's role, projects and row-policy attributes."""
    return Principal(
        user_id=context.user_id,
        role_name=context.role_name,
        capabilities=context.capabilities,
        project_ids=context.project_ids,
        attributes=context.attributes or {},
    )
