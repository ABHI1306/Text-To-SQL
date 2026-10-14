"""LDAP directory login: service bind, user search, user bind, groups and attributes.

The API finds the user's entry with a read-only service bind and then binds as that entry to
verify the password, so no password is ever stored. LDAPS or StartTLS is required unless
``allow_insecure`` is set explicitly (tests and local demos only).
"""

from __future__ import annotations

import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ldap3 import ALL_ATTRIBUTES, SIMPLE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPBindError, LDAPException, LDAPInvalidCredentialsResult
from ldap3.utils.conv import escape_filter_chars

from app.core.types import JsonValue
from app.ports.directory import DirectoryIdentity, DirectoryUnavailable
from app.ports.identity import IdentityUnavailable, VerifiedIdentity


@dataclass(frozen=True)
class LdapConfig:
    uri: str
    service_dn: str
    service_password: str
    user_base_dn: str
    user_filter: str = "(uid={username})"
    group_base_dn: str = ""
    group_filter: str = "(member={dn})"
    attribute_map: Mapping[str, str] = field(default_factory=dict)
    start_tls: bool = False
    allow_insecure: bool = False
    ca_cert_file: str | None = None
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not self.uri or not self.service_dn or not self.user_base_dn:
            raise ValueError("LDAP uri, service DN and user base DN are required")
        if "{username}" not in self.user_filter:
            raise ValueError("LDAP user filter must contain {username}")
        secure = self.uri.lower().startswith("ldaps://") or self.start_tls
        if not secure and not self.allow_insecure:
            raise ValueError("LDAP requires ldaps:// or StartTLS (or allow_insecure for tests)")


ConnectionFactory = Callable[[Server, str, str], Connection]


class LdapDirectory:
    def __init__(self, config: LdapConfig, connection_factory: ConnectionFactory | None = None) -> None:
        self._config = config
        tls = Tls(
            validate=ssl.CERT_REQUIRED,
            ca_certs_file=config.ca_cert_file,
        ) if (config.uri.lower().startswith("ldaps://") or config.start_tls) else None
        self._server = Server(
            config.uri, use_ssl=config.uri.lower().startswith("ldaps://"), tls=tls,
            connect_timeout=max(1, round(config.timeout_seconds)),
        )
        self._factory = connection_factory or self._connect

    def _connect(self, server: Server, user: str, password: str) -> Connection:
        return Connection(
            server,
            user=user,
            password=password,
            authentication=SIMPLE,
            raise_exceptions=True,
            receive_timeout=max(1, round(self._config.timeout_seconds)),
        )

    def _bound(self, user: str, password: str) -> Connection:
        connection = self._factory(self._server, user, password)
        try:
            if not connection.bound:
                connection.open()
                if self._config.start_tls:
                    connection.start_tls()
                connection.bind()
        except Exception:
            connection.unbind()
            raise
        return connection

    def authenticate(self, username: str, password: str) -> DirectoryIdentity | None:
        # An empty password would be an anonymous bind, which most servers accept.
        if not username.strip() or not password:
            return None
        try:
            entry = self._find_user(username.strip())
            if entry is None:
                return None
            dn, attributes = entry
            try:
                self._bound(dn, password).unbind()
            except (LDAPBindError, LDAPInvalidCredentialsResult):
                return None
            groups = self._groups(dn, attributes)
        except LDAPException as error:
            raise DirectoryUnavailable("The directory server is unavailable") from error
        return DirectoryIdentity(
            username=username.strip().casefold(),
            dn=dn,
            groups=groups,
            attributes=self._mapped(attributes),
        )

    def verify(self, username: str, password: str) -> VerifiedIdentity | None:
        """The shared identity-adapter shape (see tests/identity_contract.py)."""
        try:
            identity = self.authenticate(username, password)
        except DirectoryUnavailable as error:
            raise IdentityUnavailable(str(error)) from error
        if identity is None:
            return None
        return VerifiedIdentity(
            "ldap", identity.username, identity.dn, identity.groups, dict(identity.attributes)
        )

    def _find_user(self, username: str) -> tuple[str, Mapping[str, object]] | None:
        service = self._bound(self._config.service_dn, self._config.service_password)
        try:
            service.search(
                self._config.user_base_dn,
                self._config.user_filter.format(username=escape_filter_chars(username)),
                attributes=ALL_ATTRIBUTES,
                size_limit=2,
            )
            entries = list(service.entries)
        finally:
            service.unbind()
        if len(entries) != 1:  # unknown, or ambiguous: never guess between two accounts
            return None
        entry = entries[0]
        return str(entry.entry_dn), dict(entry.entry_attributes_as_dict)

    def _groups(self, dn: str, attributes: Mapping[str, object]) -> tuple[str, ...]:
        names: list[str] = [str(value) for value in _values(attributes.get("memberOf"))]
        if self._config.group_base_dn:
            service = self._bound(self._config.service_dn, self._config.service_password)
            try:
                service.search(
                    self._config.group_base_dn,
                    self._config.group_filter.format(dn=escape_filter_chars(dn)),
                    attributes=["cn"],
                )
                for entry in service.entries:
                    names.append(str(entry.entry_dn))
                    names.extend(str(cn) for cn in _values(entry.entry_attributes_as_dict.get("cn")))
            finally:
                service.unbind()
        return tuple(dict.fromkeys(names))

    def _mapped(self, attributes: Mapping[str, object]) -> dict[str, JsonValue]:
        lowered = {key.lower(): value for key, value in attributes.items()}
        mapped: dict[str, JsonValue] = {}
        for ldap_attribute, policy_attribute in self._config.attribute_map.items():
            values = [str(v) for v in _values(lowered.get(ldap_attribute.lower()))]
            if len(values) == 1:
                mapped[policy_attribute] = values[0]
            elif values:
                mapped[policy_attribute] = list(values)
        return mapped


def _values(raw: object) -> list[object]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return list(raw)
    return [raw]
