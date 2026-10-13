from dataclasses import dataclass
from datetime import datetime

from app.core.types import JsonValue


@dataclass(frozen=True)
class AuthUser:
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
    access_token: str
    refresh_token: str
    user: AuthContext
    expires_in: int
