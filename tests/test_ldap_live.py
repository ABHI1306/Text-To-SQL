"""LDAP login against the OpenLDAP demo container (compose profile identity-demo).

Skipped unless LDAP_TEST_URI is set, e.g. in the tests container:
    LDAP_TEST_URI=ldap://ldap.example.org LDAP_TEST_CA_FILE=/app/scripts/ldap/certs/ca.crt
    LDAP_TEST_SERVICE_PASSWORD=...   (the demo users' passwords are in scripts/ldap/bootstrap.ldif)
"""

from __future__ import annotations

import os

import pytest

from app.adapters.identity.ldap import LdapConfig, LdapDirectory
from app.ports.directory import DirectoryUnavailable

URI = os.getenv("LDAP_TEST_URI")
pytestmark = pytest.mark.skipif(not URI, reason="LDAP_TEST_URI is required (identity-demo container)")


def directory(**overrides: object) -> LdapDirectory:
    values: dict[str, object] = {
        "uri": URI,
        "start_tls": not (URI or "").startswith("ldaps"),
        "ca_cert_file": os.getenv("LDAP_TEST_CA_FILE"),
        "service_dn": "cn=svc-text2sql,dc=example,dc=org",
        "service_password": os.getenv("LDAP_TEST_SERVICE_PASSWORD", ""),
        "user_base_dn": "ou=people,dc=example,dc=org",
        "group_base_dn": "ou=groups,dc=example,dc=org",
        "attribute_map": {"departmentNumber": "department", "l": "region"},
    }
    values.update(overrides)
    return LdapDirectory(LdapConfig(**values))  # type: ignore[arg-type]


def test_user_bind_groups_and_attributes_come_from_the_real_directory() -> None:
    identity = directory().authenticate("asha", "demo-asha-password")
    assert identity is not None
    assert identity.dn == "uid=asha,ou=people,dc=example,dc=org"
    assert "text2sql-analysts" in identity.groups and "text2sql-dbas" not in identity.groups
    assert identity.attributes == {"department": "sales", "region": "West"}


def test_group_membership_and_a_user_without_groups() -> None:
    ravi = directory().authenticate("ravi", "demo-ravi-password")
    mina = directory().authenticate("mina", "demo-mina-password")
    assert ravi is not None and "text2sql-dbas" in ravi.groups
    assert mina is not None and not any(g.startswith("text2sql") for g in mina.groups)
    assert mina.attributes == {"region": "West"}


@pytest.mark.parametrize(
    ("username", "password"),
    [("asha", "wrong"), ("ghost", "x"), ("asha", ""), ("asha)(uid=*", "demo-asha-password"), ("*", "x")],
)
def test_bad_credentials_are_refused(username: str, password: str) -> None:
    assert directory().authenticate(username, password) is None


def test_a_wrong_service_password_is_an_outage_not_a_login_failure() -> None:
    with pytest.raises(DirectoryUnavailable):
        directory(service_password="not-the-service-password").authenticate("asha", "demo-asha-password")


def test_certificates_are_verified() -> None:
    with pytest.raises(DirectoryUnavailable):
        directory(ca_cert_file=None).authenticate("asha", "demo-asha-password")
