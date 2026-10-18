"""OpenID Connect login: authorization code flow with PKCE and full ID-token validation.

Authlib drives the redirect and token exchange; joserfc (Authlib's JOSE implementation) checks
the ID token. Nothing here stores anything: the caller keeps state, nonce and the PKCE verifier
for the one login in flight. The ID token is accepted only when its signature verifies against
the provider's JWKS (asymmetric algorithms only) and ``iss``, ``aud``/``azp``, ``exp``, ``nbf``
and ``nonce`` all check out. Discovery and keys are fetched over TLS unless ``allow_insecure``
is set explicitly (local Keycloak demo only).
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import quote, urlparse

import httpx
from authlib.common.security import generate_token
from authlib.integrations.httpx_client import OAuth2Client
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from joserfc.jwt import JWTClaimsRegistry

from app.core.types import JsonValue
from app.ports.identity import (
    AuthorizationRequest,
    IdentityRejected,
    IdentityUnavailable,
    VerifiedIdentity,
)

_ASYMMETRIC_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "ES256", "ES384", "EdDSA")
_METADATA_TTL_SECONDS = 3600
_JWKS_MIN_REFRESH_SECONDS = 30
_MAX_USERNAME_LENGTH = 128


@dataclass(frozen=True)
class OidcConfig:
    """OIDC client settings; ``https`` is required unless ``allow_insecure`` is set."""
    issuer: str
    client_id: str
    redirect_uri: str
    client_secret: str = field(default="", repr=False)
    discovery_url: str = ""
    scopes: tuple[str, ...] = ("openid", "profile", "email")
    username_claim: str = "preferred_username"
    groups_claim: str = "groups"
    attribute_map: Mapping[str, str] = field(default_factory=dict)
    allow_insecure: bool = False
    leeway_seconds: int = 60
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        """Reject missing fields, scopes without ``openid`` and non-https URLs."""
        if not self.issuer or not self.client_id or not self.redirect_uri:
            raise ValueError("OIDC issuer, client id and redirect URI are required")
        if "openid" not in self.scopes:
            raise ValueError("OIDC scopes must include openid")
        if not self.allow_insecure:
            for name, url in (("issuer", self.issuer), ("redirect URI", self.redirect_uri),
                              ("discovery URL", self.discovery_url)):
                if url and urlparse(url).scheme != "https":
                    raise ValueError(f"OIDC {name} must use https (or set allow_insecure)")


@dataclass
class _Metadata:
    """The discovery endpoints needed for login, with the time they were fetched."""
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    fetched_at: float


class OidcProvider:
    """OIDC authorization-code-with-PKCE login that validates the ID token strictly."""
    source = "oidc"

    def __init__(
        self,
        config: OidcConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Store the config and optional HTTP transport; discovery and keys load lazily."""
        self._config = config
        self._transport = transport
        self._lock = threading.Lock()
        self._metadata: _Metadata | None = None
        self._keys: KeySet | None = None
        self._keys_fetched_at = 0.0

    # ---- discovery and keys -------------------------------------------------------------

    def _http(self) -> httpx.Client:
        """Return an HTTP client with a timeout that never follows redirects."""
        return httpx.Client(
            transport=self._transport,
            timeout=self._config.timeout_seconds,
            follow_redirects=False,
        )

    def _discover(self) -> _Metadata:
        """Fetch or reuse (cached) the discovery document, checking its issuer matches."""
        with self._lock:
            cached = self._metadata
            if cached is not None and time.monotonic() - cached.fetched_at < _METADATA_TTL_SECONDS:
                return cached
        url = self._config.discovery_url or (
            self._config.issuer.rstrip("/") + "/.well-known/openid-configuration"
        )
        document = self._get_json(url)
        if document.get("issuer") != self._config.issuer:
            raise IdentityUnavailable("OIDC discovery document names a different issuer")
        try:
            metadata = _Metadata(
                authorization_endpoint=str(document["authorization_endpoint"]),
                token_endpoint=str(document["token_endpoint"]),
                jwks_uri=str(document["jwks_uri"]),
                fetched_at=time.monotonic(),
            )
        except KeyError as error:
            raise IdentityUnavailable("OIDC discovery document is incomplete") from error
        with self._lock:
            self._metadata = metadata
        return metadata

    def _get_json(self, url: str) -> dict[str, Any]:
        """GET a JSON object, mapping transport and shape errors to ``IdentityUnavailable``."""
        try:
            with self._http() as client:
                response = client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise IdentityUnavailable("OIDC provider could not be reached") from error
        if not isinstance(body, dict):
            raise IdentityUnavailable("OIDC provider returned an unexpected document")
        return body

    def _key_set(self, *, force: bool = False) -> KeySet:
        """Return the cached JWKS, refetching on force but rate-limited to avoid fetch floods."""
        with self._lock:
            keys = self._keys
            age = time.monotonic() - self._keys_fetched_at
        if keys is not None and not force:
            return keys
        if keys is not None and age < _JWKS_MIN_REFRESH_SECONDS:
            return keys  # a flood of unknown key ids must not turn into a flood of fetches
        document = self._get_json(self._discover().jwks_uri)
        try:
            fresh = KeySet.import_key_set(cast(Any, document))
        except (JoseError, ValueError, KeyError, TypeError) as error:
            raise IdentityUnavailable("OIDC provider published an unusable key set") from error
        with self._lock:
            self._keys = fresh
            self._keys_fetched_at = time.monotonic()
        return fresh

    # ---- the flow -------------------------------------------------------------------------

    def _client(self) -> OAuth2Client:
        """Build the Authlib client configured for PKCE (S256) and the client auth method."""
        return OAuth2Client(
            client_id=self._config.client_id,
            client_secret=self._config.client_secret or None,
            token_endpoint_auth_method=(
                "client_secret_basic" if self._config.client_secret else "none"
            ),
            redirect_uri=self._config.redirect_uri,
            scope=" ".join(self._config.scopes),
            code_challenge_method="S256",
        )

    def begin(self) -> AuthorizationRequest:
        """Start a login: generate state, nonce and PKCE verifier and the redirect URL."""
        metadata = self._discover()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = generate_token(64)
        with self._client() as client:
            url, _ = client.create_authorization_url(
                metadata.authorization_endpoint,
                state=state,
                nonce=nonce,
                code_verifier=verifier,
            )
        return AuthorizationRequest(url=url, state=state, nonce=nonce, code_verifier=verifier)

    def _exchange(self, token_endpoint: str, code: str, code_verifier: str) -> Mapping[str, Any]:
        """The token request (RFC 6749 4.1.3 with the PKCE verifier from RFC 7636)."""
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._config.redirect_uri,
            "code_verifier": code_verifier,
        }
        auth: Any = None
        if self._config.client_secret:
            auth = httpx.BasicAuth(
                quote(self._config.client_id, safe=""), quote(self._config.client_secret, safe="")
            )
        else:
            form["client_id"] = self._config.client_id
        try:
            with self._http() as client:
                response = client.post(
                    token_endpoint, data=form, auth=auth, headers={"Accept": "application/json"}
                )
        except httpx.HTTPError as error:
            raise IdentityUnavailable("OIDC token endpoint could not be reached") from error
        if response.status_code >= 500:
            raise IdentityUnavailable("OIDC token endpoint failed")
        if response.status_code != 200:
            raise IdentityRejected("token endpoint refused the authorization code")
        try:
            body = response.json()
        except ValueError as error:
            raise IdentityUnavailable("OIDC token endpoint returned a malformed response") from error
        if not isinstance(body, dict):
            raise IdentityUnavailable("OIDC token endpoint returned a malformed response")
        return body

    def complete(self, *, code: str, nonce: str, code_verifier: str) -> VerifiedIdentity:
        """Exchange the code, validate the ID token and return the verified identity."""
        if not code or not nonce or not code_verifier:
            raise IdentityRejected("missing authorization code")
        metadata = self._discover()
        token = self._exchange(metadata.token_endpoint, code, code_verifier)
        id_token = token.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise IdentityRejected("token response carries no ID token")
        claims = self._validated_claims(id_token, nonce)
        return self._identity(claims)

    # ---- validation -----------------------------------------------------------------------

    def _validated_claims(self, id_token: str, nonce: str) -> Mapping[str, Any]:
        """Verify signature (keys refreshed once), ``iss``, ``exp``, audience, ``azp``, nonce."""
        try:
            decoded = self._decode(id_token, self._key_set())
        except JoseError:
            # An unknown key id usually means the provider rotated keys: refresh once.
            try:
                decoded = self._decode(id_token, self._key_set(force=True))
            except JoseError as error:
                raise IdentityRejected("ID token signature or format is invalid") from error
        claims = decoded.claims
        registry = JWTClaimsRegistry(
            leeway=self._config.leeway_seconds,
            iss={"essential": True, "value": self._config.issuer},
            sub={"essential": True},
            exp={"essential": True},
            nbf={"essential": False},
        )
        try:
            registry.validate(claims)
        except JoseError as error:
            raise IdentityRejected("ID token claims are invalid") from error
        audience = claims.get("aud")
        audiences = [audience] if isinstance(audience, str) else audience
        if not isinstance(audiences, list) or self._config.client_id not in audiences:
            raise IdentityRejected("ID token audience does not include this client")
        if len(audiences) > 1 and claims.get("azp") != self._config.client_id:
            raise IdentityRejected("ID token authorized party is not this client")
        token_nonce = claims.get("nonce")
        if not isinstance(token_nonce, str) or not hmac.compare_digest(
            token_nonce.encode(), nonce.encode()
        ):
            raise IdentityRejected("ID token nonce does not match the login request")
        return claims

    def _decode(self, id_token: str, keys: KeySet) -> jwt.Token:
        """Decode the ID token against the key set, allowing asymmetric algorithms only."""
        try:
            return jwt.decode(id_token, keys, algorithms=list(_ASYMMETRIC_ALGORITHMS))
        except (ValueError, TypeError, KeyError) as error:
            raise IdentityRejected("ID token is malformed") from error

    def _identity(self, claims: Mapping[str, Any]) -> VerifiedIdentity:
        """Build the identity from claims: username, groups and mapped attributes."""
        raw_username = _claim(claims, self._config.username_claim)
        username = str(raw_username).strip().casefold() if isinstance(raw_username, str) else ""
        if not username or len(username) > _MAX_USERNAME_LENGTH or any(
            character.isspace() or ord(character) < 32 for character in username
        ):
            raise IdentityRejected("ID token has no usable username claim")
        groups = tuple(
            dict.fromkeys(str(item) for item in _as_list(_claim(claims, self._config.groups_claim)))
        )
        attributes: dict[str, JsonValue] = {}
        for claim_name, attribute in self._config.attribute_map.items():
            values = [
                item for item in _as_list(_claim(claims, claim_name))
                if isinstance(item, (str, int, float, bool))
            ]
            if len(values) == 1:
                attributes[attribute] = values[0]
            elif values:
                attributes[attribute] = list(values)
        return VerifiedIdentity("oidc", username, str(claims["sub"]), groups, attributes)


def _claim(claims: Mapping[str, Any], path: str) -> Any:
    """A claim by name, or by dotted path for nested claims such as ``realm_access.roles``."""
    if path in claims:
        return claims[path]
    current: Any = claims
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _as_list(raw: Any) -> list[Any]:
    """Wrap a claim value in a list (``None`` becomes empty)."""
    if raw is None:
        return []
    return list(raw) if isinstance(raw, (list, tuple)) else [raw]
