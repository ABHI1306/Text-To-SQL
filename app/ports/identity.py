from collections.abc import Mapping
from typing import Protocol

from app.core.types import JsonValue, Principal


class IdentityPort(Protocol):
    def authenticate_password(
        self, username: str, password: str
    ) -> Principal | None: ...

    def resolve_external_identity(
        self,
        provider: str,
        subject: str,
        claims: Mapping[str, JsonValue],
    ) -> Principal: ...