"""Port for verifying credentials against a directory server such as LDAP."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from app.core.errors import PortUnavailable
from app.core.types import JsonValue


class DirectoryUnavailable(PortUnavailable):
    """The directory server could not be reached or answered with a protocol error."""


@dataclass(frozen=True)
class DirectoryIdentity:
    """A directory user whose password was verified: name, DN, groups and attributes."""
    username: str
    dn: str
    groups: tuple[str, ...]
    attributes: Mapping[str, JsonValue]


class DirectoryPort(Protocol):
    """Directory server login; implementations raise ``DirectoryUnavailable`` when it is down."""
    def authenticate(self, username: str, password: str) -> DirectoryIdentity | None:
        """The verified identity, or None for an unknown user or a wrong password."""
        ...
