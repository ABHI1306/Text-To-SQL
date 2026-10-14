"""A local OpenID provider: real RSA keys, signed ID tokens, discovery, JWKS and a token endpoint.

It runs behind httpx.MockTransport, so the OIDC adapter exercises its real HTTP and JOSE paths
without a network. The token endpoint enforces PKCE and client authentication like a real one.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from joserfc import jwt
from joserfc.jwk import RSAKey

ISSUER = "https://idp.example.test/realms/text2sql"
CLIENT_ID = "text2sql-api"
CLIENT_SECRET = "client-secret-for-tests"
REDIRECT_URI = "https://app.example.test/api/v1/auth/oidc/callback"


class FakeIdp:
    def __init__(self) -> None:
        self.key = RSAKey.generate_key(2048, {"kid": "key-1", "use": "sig"})
        self.rogue_key = RSAKey.generate_key(2048, {"kid": "key-1", "use": "sig"})
        self.published = [self.key]
        self.users: dict[str, dict[str, Any]] = {}
        self.codes: dict[str, dict[str, Any]] = {}
        self.down = False
        self.discovery_issuer = ISSUER
        self.token_overrides: dict[str, Any] = {}
        self.signing_key = self.key
        self.signing_alg = "RS256"
        self.drop_id_token = False
        self.jwks_fetches = 0
        self.token_calls = 0

    def add_user(self, username: str, **claims: Any) -> None:
        self.users[username] = {"preferred_username": username, "sub": f"sub-{username}", **claims}

    # ---- the browser leg ----------------------------------------------------------------

    def authorize(self, url: str, username: str) -> str:
        """Sign the user in and return the authorization code, as the redirect back would."""
        query = {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}
        assert query["response_type"] == "code"
        assert query["client_id"] == CLIENT_ID
        assert query["redirect_uri"] == REDIRECT_URI
        assert query["code_challenge_method"] == "S256"
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"query": query, "username": username}
        return code

    # ---- the HTTP leg -------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("idp is down", request=request)
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "issuer": self.discovery_issuer,
                "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
                "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
                "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
            })
        if path.endswith("/certs"):
            self.jwks_fetches += 1
            return httpx.Response(200, json={"keys": [k.as_dict(private=False) for k in self.published]})
        if path.endswith("/token"):
            return self._token(request)
        return httpx.Response(404)

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_calls += 1
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if request.headers.get("authorization") != expected:
            return httpx.Response(401, json={"error": "invalid_client"})
        grant = self.codes.pop(form.get("code", ""), None)  # a code works once
        if grant is None or form.get("grant_type") != "authorization_code":
            return httpx.Response(400, json={"error": "invalid_grant"})
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        ).rstrip(b"=").decode()
        if challenge != grant["query"]["code_challenge"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300,
            "nonce": grant["query"]["nonce"], **self.users[grant["username"]],
        }
        claims.update(self.token_overrides)
        claims = {k: v for k, v in claims.items() if v is not _MISSING}
        body: dict[str, Any] = {"access_token": "opaque", "token_type": "Bearer", "expires_in": 300}
        if not self.drop_id_token:
            body["id_token"] = self.sign(claims)
        return httpx.Response(200, json=body)

    def sign(self, claims: Mapping[str, Any]) -> str:
        return jwt.encode({"alg": self.signing_alg, "kid": "key-1"}, dict(claims), self.signing_key)


_MISSING: Any = object()
MISSING = _MISSING


def unsigned_token(claims: Mapping[str, Any]) -> str:
    def part(data: Mapping[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}."
