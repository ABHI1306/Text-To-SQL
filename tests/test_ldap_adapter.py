"""LDAP adapter against ldap3's in-memory mock server (the OpenLDAP test is in test_ldap_live)."""

from __future__ import annotations

import pytest
from ldap3 import MOCK_SYNC, Connection, Server

from app.adapters.identity.ldap import LdapConfig, LdapDirectory
from app.ports.directory import DirectoryUnavailable

BASE = "dc=example,dc=org"
SERVICE = f"cn=svc,{BASE}"


def mock_directory(**overrides: object) -> LdapDirectory:
    config = LdapConfig(
        uri="ldap://mock",
        service_dn=SERVICE,
        service_password="service-secret",
        user_base_dn=f"ou=people,{BASE}",
        group_base_dn=f"ou=groups,{BASE}",
        attribute_map={"departmentNumber": "department", "l": "region", "mail": "emails"},
        allow_insecure=True,
        **overrides,  # type: ignore[arg-type]
    )

    def factory(server: Server, user: str, password: str) -> Connection:
        return Connection(
            server, user=user, password=password, client_strategy=MOCK_SYNC, raise_exceptions=True
        )

    directory = LdapDirectory(config, factory)
    seed = factory(directory._server, SERVICE, "service-secret")
    seed.strategy.add_entry(SERVICE, {"userPassword": "service-secret", "objectClass": "person"})
    for uid, password, extra in (
        ("asha", "asha-secret", {"departmentNumber": "sales", "l": "West", "mail": ["a@x.test", "b@x.test"]}),
        ("ravi", "ravi-secret", {"departmentNumber": "ops", "l": "South"}),
    ):
        seed.strategy.add_entry(
            f"uid={uid},ou=people,{BASE}",
            {"uid": uid, "userPassword": password, "objectClass": "inetOrgPerson", **extra},
        )
    seed.strategy.add_entry(
        f"cn=analysts,ou=groups,{BASE}",
        {"cn": "analysts", "objectClass": "groupOfNames", "member": f"uid=asha,ou=people,{BASE}"},
    )
    seed.strategy.add_entry(
        f"cn=dbas,ou=groups,{BASE}",
        {"cn": "dbas", "objectClass": "groupOfNames", "member": f"uid=ravi,ou=people,{BASE}"},
    )
    seed.unbind()
    return directory


def test_valid_credentials_return_identity_groups_and_mapped_attributes() -> None:
    identity = mock_directory().authenticate("asha", "asha-secret")
    assert identity is not None
    assert identity.dn == f"uid=asha,ou=people,{BASE}"
    assert identity.username == "asha"
    assert "analysts" in identity.groups and f"cn=analysts,ou=groups,{BASE}" in identity.groups
    assert "dbas" not in identity.groups
    assert identity.attributes == {
        "department": "sales",
        "region": "West",
        "emails": ["a@x.test", "b@x.test"],
    }


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("asha", "wrong"),
        ("ghost", "asha-secret"),
        ("asha", ""),  # empty password would be an anonymous bind
        ("", "asha-secret"),
        ("   ", "x"),
        ("asha)(uid=*", "asha-secret"),  # filter injection is escaped, so it matches nobody
        ("*", "asha-secret"),
        ("ravi", "asha-secret"),
    ],
)
def test_bad_credentials_never_authenticate(username: str, password: str) -> None:
    assert mock_directory().authenticate(username, password) is None


def test_username_lookup_is_case_insensitive_in_the_result() -> None:
    identity = mock_directory().authenticate("ASHA", "asha-secret")
    assert identity is not None and identity.username == "asha"


def test_wrong_service_credentials_surface_as_unavailable_not_as_a_login_failure() -> None:
    directory = mock_directory()
    broken = LdapDirectory(
        LdapConfig(
            uri="ldap://mock", service_dn=SERVICE, service_password="nope",
            user_base_dn=f"ou=people,{BASE}", allow_insecure=True,
        ),
        directory._factory,
    )
    with pytest.raises(DirectoryUnavailable):
        broken.authenticate("asha", "asha-secret")


def test_plain_ldap_is_refused_unless_explicitly_allowed() -> None:
    kwargs = {"service_dn": SERVICE, "service_password": "x", "user_base_dn": BASE}
    with pytest.raises(ValueError, match="ldaps"):
        LdapConfig(uri="ldap://dir", **kwargs)
    LdapConfig(uri="ldaps://dir", **kwargs)
    LdapConfig(uri="ldap://dir", start_tls=True, **kwargs)


def test_config_requires_a_username_placeholder() -> None:
    with pytest.raises(ValueError, match="username"):
        LdapConfig(
            uri="ldaps://dir", service_dn=SERVICE, service_password="x",
            user_base_dn=BASE, user_filter="(uid=fixed)",
        )
