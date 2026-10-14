"""OIDC login against the Keycloak demo realm (docker-compose.oidc.yml, profile identity-demo).

Skipped unless OIDC_TEST_ISSUER is set and the container answers, e.g.:
    OIDC_TEST_ISSUER=http://localhost:8081/realms/text2sql OIDC_TEST_CLIENT_SECRET=...
The demo users' passwords are in scripts/oidc/realm-text2sql.json.
"""

from __future__ import annotations

import html
import os
import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.adapters.identity.oidc import OidcConfig, OidcProvider
from app.ports.identity import IdentityRejected

ISSUER = os.getenv("OIDC_TEST_ISSUER", "")
REDIRECT = "http://localhost:8000/api/v1/auth/oidc/callback"


def _keycloak_up() -> bool:
    if not ISSUER:
        return False
    try:
        return httpx.get(
            ISSUER + "/.well-known/openid-configuration", timeout=2
        ).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(
    not _keycloak_up(), reason="OIDC_TEST_ISSUER must point at a running Keycloak (identity-demo)"
)


def provider() -> OidcProvider:
    return OidcProvider(
        OidcConfig(
            issuer=ISSUER,
            client_id=os.getenv("OIDC_TEST_CLIENT_ID", "text2sql-api"),
            client_secret=os.getenv("OIDC_TEST_CLIENT_SECRET", ""),
            redirect_uri=REDIRECT,
            attribute_map={"region": "region", "department": "department"},
            allow_insecure=ISSUER.startswith("http://"),
        )
    )


def browser_login(url: str, username: str, password: str) -> str | None:
    """Drive Keycloak's login form like a browser; return the authorization code or None."""
    with httpx.Client(follow_redirects=False, timeout=10) as browser:
        page = browser.get(url)
        match = re.search(r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"', page.text)
        assert match, "Keycloak login form not found"
        # Keycloak marks its cookies Secure even over http; browsers still send them to
        # localhost but Python's cookie jar does not, so replay them by hand.
        cookies = "; ".join(
            h.split(";", 1)[0] for h in page.headers.get_list("set-cookie")
        )
        response = browser.post(
            html.unescape(match.group(1)),
            data={"username": username, "password": password},
            headers={"Cookie": cookies},
        )
        location = response.headers.get("location", "")
        if not location.startswith(REDIRECT):
            return None
        codes = parse_qs(urlparse(location).query).get("code")
        return codes[0] if codes else None


def test_a_real_keycloak_login_yields_groups_and_attributes() -> None:
    p = provider()
    request = p.begin()
    code = browser_login(request.url, "asha", "demo-asha-password")
    assert code
    identity = p.complete(code=code, nonce=request.nonce, code_verifier=request.code_verifier)
    assert identity.username == "asha" and identity.source == "oidc"
    assert "text2sql-analysts" in identity.groups and "text2sql-dbas" not in identity.groups
    assert identity.attributes == {"region": "West", "department": "sales"}


def test_a_person_without_groups_has_none() -> None:
    p = provider()
    request = p.begin()
    code = browser_login(request.url, "mina", "demo-mina-password")
    assert code
    assert p.complete(
        code=code, nonce=request.nonce, code_verifier=request.code_verifier
    ).groups == ()


def test_a_wrong_password_never_produces_a_code() -> None:
    assert browser_login(provider().begin().url, "asha", "not-the-password") is None


def test_the_code_is_bound_to_this_logins_pkce_verifier_and_nonce() -> None:
    p = provider()
    request = p.begin()
    code = browser_login(request.url, "ravi", "demo-ravi-password")
    assert code
    with pytest.raises(IdentityRejected):
        p.complete(code=code, nonce=request.nonce, code_verifier="x" * 64)
    other = p.begin()
    code = browser_login(other.url, "ravi", "demo-ravi-password")
    assert code
    with pytest.raises(IdentityRejected):  # another login's nonce
        p.complete(code=code, nonce=request.nonce, code_verifier=other.code_verifier)
