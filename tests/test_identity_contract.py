"""Both identity adapters (LDAP over ldap3's mock server, OIDC against a local fake IdP) must pass
the same contract."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import parse_qs

from ldap3.core.exceptions import LDAPSocketOpenError

from app.adapters.identity.oidc import OidcConfig, OidcProvider
from app.core.types import JsonValue
from app.ports.identity import IdentityRejected, VerifiedIdentity
from tests.support.fake_idp import (
    CLIENT_ID,
    CLIENT_SECRET,
    ISSUER,
    REDIRECT_URI,
    FakeIdp,
)
from tests.support.identity_contract import IdentityContract, IdentityHarness
from tests.test_ldap_adapter import BASE, SERVICE, mock_directory


class LdapHarness:
    source = "ldap"

    def __init__(self) -> None:
        self.directory = mock_directory()
        self._seed = self.directory._factory(self.directory._server, SERVICE, "service-secret")
        self._seed.bind()

    def add_user(
        self, username: str, secret: str, groups: tuple[str, ...], attributes: Mapping[str, JsonValue]
    ) -> None:
        dn = f"uid={username},ou=people,{BASE}"
        entry: dict[str, object] = {
            "uid": username, "userPassword": secret, "objectClass": "inetOrgPerson",
        }
        if "region" in attributes:
            entry["l"] = attributes["region"]
        self._seed.strategy.add_entry(dn, entry)
        for group in groups:
            self._seed.strategy.add_entry(
                f"cn={group},ou=groups,{BASE}",
                {"cn": group, "objectClass": "groupOfNames", "member": dn},
            )

    def verify(self, username: str, secret: str) -> VerifiedIdentity | None:
        return self.directory.verify(username, secret)

    def break_backend(self) -> None:
        def down(*_: object) -> object:
            raise LDAPSocketOpenError("directory down")

        self.directory._factory = down  # type: ignore[assignment]


class OidcHarness:
    source = "oidc"

    def __init__(self) -> None:
        self.idp = FakeIdp()
        self.secrets: dict[str, str] = {}
        self.provider = OidcProvider(
            OidcConfig(
                issuer=ISSUER, client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                redirect_uri=REDIRECT_URI, attribute_map={"region": "region"},
            ),
            transport=self.idp.transport(),
        )

    def add_user(
        self, username: str, secret: str, groups: tuple[str, ...], attributes: Mapping[str, JsonValue]
    ) -> None:
        self.secrets[username] = secret
        self.idp.add_user(username, groups=list(groups), **attributes)

    def verify(self, username: str, secret: str) -> VerifiedIdentity | None:
        request = self.provider.begin()
        assert parse_qs(request.url.split("?", 1)[1])["state"] == [request.state]
        if self.secrets.get(username) != secret:
            code = "forged-code"  # the IdP never issued it: the proof of login is wrong
        else:
            code = self.idp.authorize(request.url, username)
        try:
            return self.provider.complete(
                code=code, nonce=request.nonce, code_verifier=request.code_verifier
            )
        except IdentityRejected:
            return None

    def break_backend(self) -> None:
        self.idp.down = True


class TestLdapContract(IdentityContract):
    def harness(self) -> IdentityHarness:
        return LdapHarness()


class TestOidcContract(IdentityContract):
    def harness(self) -> IdentityHarness:
        return OidcHarness()
