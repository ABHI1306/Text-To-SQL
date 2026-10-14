"""OIDC adapter: PKCE, state/nonce material, and every ID-token check, against a local fake IdP."""

from __future__ import annotations

import time
from typing import Any

import pytest
from joserfc import jwt
from joserfc.jwk import OctKey

from app.adapters.identity.oidc import OidcConfig, OidcProvider
from app.ports.identity import AuthorizationRequest, IdentityRejected, IdentityUnavailable
from tests.support.fake_idp import (
    CLIENT_ID,
    CLIENT_SECRET,
    ISSUER,
    MISSING,
    REDIRECT_URI,
    FakeIdp,
    unsigned_token,
)


def make(idp: FakeIdp, **overrides: Any) -> OidcProvider:
    values: dict[str, Any] = {
        "issuer": ISSUER, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "attribute_map": {"region": "region", "department": "department"},
    }
    values.update(overrides)
    return OidcProvider(OidcConfig(**values), transport=idp.transport())


def login(idp: FakeIdp, provider: OidcProvider, user: str = "asha") -> tuple[AuthorizationRequest, str]:
    request = provider.begin()
    return request, idp.authorize(request.url, user)


def setup() -> tuple[FakeIdp, OidcProvider]:
    idp = FakeIdp()
    idp.add_user("asha", groups=["analysts"], region="West", department=["a", "b"])
    return idp, make(idp)


def test_the_happy_path_returns_groups_and_mapped_attributes() -> None:
    idp, provider = setup()
    request, code = login(idp, provider)
    identity = provider.complete(code=code, nonce=request.nonce, code_verifier=request.code_verifier)
    assert (identity.username, identity.subject, identity.groups) == ("asha", "sub-asha", ("analysts",))
    assert identity.attributes == {"region": "West", "department": ["a", "b"]}


def test_every_authorization_request_has_fresh_state_nonce_and_pkce_verifier() -> None:
    _, provider = setup()
    a, b = provider.begin(), provider.begin()
    assert len({a.state, b.state}) == len({a.nonce, b.nonce}) == len({a.code_verifier, b.code_verifier}) == 2
    assert len(a.state) >= 32 and len(a.code_verifier) >= 43
    assert "code_challenge_method=S256" in a.url and a.code_verifier not in a.url


def test_pkce_the_wrong_verifier_is_refused_by_the_token_endpoint() -> None:
    idp, provider = setup()
    request, code = login(idp, provider)
    with pytest.raises(IdentityRejected):
        provider.complete(code=code, nonce=request.nonce, code_verifier="x" * 64)


def test_a_code_cannot_be_replayed() -> None:
    idp, provider = setup()
    request, code = login(idp, provider)
    provider.complete(code=code, nonce=request.nonce, code_verifier=request.code_verifier)
    with pytest.raises(IdentityRejected):
        provider.complete(code=code, nonce=request.nonce, code_verifier=request.code_verifier)


@pytest.mark.parametrize(
    ("override", "why"),
    [
        ({"nonce": "someone-elses"}, "nonce"),
        ({"nonce": MISSING}, "missing nonce"),
        ({"aud": "another-client"}, "audience"),
        ({"aud": ["another-client", "third"]}, "audience list without us"),
        ({"aud": [CLIENT_ID, "other"]}, "multi audience without azp"),
        ({"aud": MISSING}, "missing audience"),
        ({"iss": "https://evil.example.test/realms/x"}, "issuer"),
        ({"iss": MISSING}, "missing issuer"),
        ({"exp": int(time.time()) - 3600}, "expired"),
        ({"exp": MISSING}, "missing exp"),
        ({"nbf": int(time.time()) + 3600}, "not yet valid"),
        ({"sub": MISSING}, "missing sub"),
        ({"preferred_username": MISSING}, "no username"),
        ({"preferred_username": "two words"}, "unusable username"),
    ],
)
def test_id_token_claim_failures_are_rejected(override: dict[str, Any], why: str) -> None:
    idp, provider = setup()
    idp.token_overrides = override
    request, code = login(idp, provider)
    with pytest.raises(IdentityRejected):
        provider.complete(code=code, nonce=request.nonce, code_verifier=request.code_verifier)


def test_a_multi_audience_token_is_accepted_only_with_us_as_authorized_party() -> None:
    idp, provider = setup()
    idp.token_overrides = {"aud": [CLIENT_ID, "other"], "azp": CLIENT_ID}
    request, code = login(idp, provider)
    assert provider.complete(code=code, nonce=request.nonce, code_verifier=request.code_verifier)


def test_a_token_signed_by_an_unpublished_key_is_rejected() -> None:
    idp, provider = setup()
    idp.signing_key = idp.rogue_key  # same kid, different key material
    request, code = login(idp, provider)
    with pytest.raises(IdentityRejected):
        provider.complete(code=code, nonce=request.nonce, code_verifier=request.code_verifier)


def test_the_none_algorithm_is_rejected() -> None:
    idp, provider = setup()
    idp.sign = lambda claims: unsigned_token(claims)  # type: ignore[method-assign]
    request, code = login(idp, provider)
    with pytest.raises(IdentityRejected):
        provider.complete(code=code, nonce=request.nonce, code_verifier=request.code_verifier)


def test_a_symmetric_algorithm_cannot_be_forced_with_the_public_key() -> None:
    idp, provider = setup()
    public_pem = idp.key.as_pem(private=False)
    idp.signing_key = OctKey.import_key(public_pem)  # type: ignore[assignment]
    idp.signing_alg = "HS256"
    request, code = login(idp, provider)
    with pytest.raises(IdentityRejected):
        provider.complete(code=code, nonce=request.nonce, code_verifier=request.code_verifier)


def test_a_missing_id_token_is_rejected() -> None:
    idp, provider = setup()
    idp.drop_id_token = True
    request, code = login(idp, provider)
    with pytest.raises(IdentityRejected):
        provider.complete(code=code, nonce=request.nonce, code_verifier=request.code_verifier)


def test_a_malformed_id_token_is_rejected() -> None:
    idp, provider = setup()
    idp.sign = lambda claims: "not.a.jwt"  # type: ignore[method-assign]
    request, code = login(idp, provider)
    with pytest.raises(IdentityRejected):
        provider.complete(code=code, nonce=request.nonce, code_verifier=request.code_verifier)


def test_key_rotation_is_picked_up_by_one_refetch() -> None:
    idp, provider = setup()
    request, code = login(idp, provider)
    provider.complete(code=code, nonce=request.nonce, code_verifier=request.code_verifier)
    assert idp.jwks_fetches == 1
    new_key = type(idp.key).generate_key(2048, {"kid": "key-2", "use": "sig"})
    idp.published, idp.signing_key = [new_key], new_key
    idp.sign = lambda claims: jwt.encode({"alg": "RS256", "kid": "key-2"}, dict(claims), new_key)  # type: ignore[method-assign]
    provider._keys_fetched_at -= 120  # the refetch limiter allows another fetch
    request, code = login(idp, provider)
    provider.complete(code=code, nonce=request.nonce, code_verifier=request.code_verifier)
    assert idp.jwks_fetches == 2


def test_unknown_key_ids_cannot_force_a_fetch_storm() -> None:
    idp, provider = setup()
    idp.sign = lambda claims: jwt.encode(  # type: ignore[method-assign]
        {"alg": "RS256", "kid": "ghost"}, dict(claims), idp.rogue_key
    )
    for _ in range(3):
        request, code = login(idp, provider)
        with pytest.raises(IdentityRejected):
            provider.complete(code=code, nonce=request.nonce, code_verifier=request.code_verifier)
    assert idp.jwks_fetches <= 2


def test_a_discovery_document_for_a_different_issuer_is_not_trusted() -> None:
    idp, provider = setup()
    idp.discovery_issuer = "https://evil.example.test"
    with pytest.raises(IdentityUnavailable):
        provider.begin()


def test_provider_outage_is_unavailable_not_rejected() -> None:
    idp, provider = setup()
    request, code = login(idp, provider)
    idp.down = True
    with pytest.raises(IdentityUnavailable):
        provider.complete(code=code, nonce=request.nonce, code_verifier=request.code_verifier)


def test_https_is_required_unless_explicitly_relaxed() -> None:
    with pytest.raises(ValueError, match="https"):
        OidcConfig(issuer="http://idp.test/r", client_id="c", redirect_uri="https://a/cb")
    with pytest.raises(ValueError, match="https"):
        OidcConfig(issuer="https://idp.test/r", client_id="c", redirect_uri="http://a/cb")
    OidcConfig(issuer="http://idp.test/r", client_id="c", redirect_uri="http://a/cb", allow_insecure=True)
    with pytest.raises(ValueError, match="openid"):
        OidcConfig(issuer="https://i", client_id="c", redirect_uri="https://a", scopes=("email",))


def test_the_client_secret_is_not_in_the_config_repr() -> None:
    assert CLIENT_SECRET not in repr(OidcConfig(
        issuer=ISSUER, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, redirect_uri=REDIRECT_URI))


def test_nested_group_claims_and_scalar_groups_are_read() -> None:
    idp = FakeIdp()
    idp.add_user("ravi", realm_access={"roles": ["dba", "dba", "x"]}, team="ops")
    provider = make(idp, groups_claim="realm_access.roles")
    request, code = login(idp, provider, "ravi")
    identity = provider.complete(code=code, nonce=request.nonce, code_verifier=request.code_verifier)
    assert identity.groups == ("dba", "x")
    provider = make(idp, groups_claim="team")
    request, code = login(idp, provider, "ravi")
    assert provider.complete(code=code, nonce=request.nonce, code_verifier=request.code_verifier).groups == ("ops",)
