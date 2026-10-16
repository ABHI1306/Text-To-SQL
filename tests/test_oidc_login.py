"""OIDC login through OidcLoginService, AuthService and the HTTP routes (fake IdP, fake repo)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.identity.oidc import OidcConfig, OidcProvider
from app.api import dependencies
from app.api.oidc_routes import STATE_COOKIE, router
from app.auth.oidc import InMemoryOidcStateStore, OidcLoginService, state_hash
from app.auth.service import AuthenticationError, AuthService
from tests.support.fake_idp import CLIENT_ID, CLIENT_SECRET, ISSUER, REDIRECT_URI, FakeIdp
from tests.test_ldap_login import SECRET, Repo


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


class World:
    def __init__(self) -> None:
        self.idp = FakeIdp()
        self.idp.add_user("asha", groups=["analysts"], region="West")
        self.idp.add_user("ravi", groups=["dbas", "analysts"])
        self.idp.add_user("mina", groups=["interns"])
        self.repo = Repo()
        self.repo.mappings = [("dbas", "DBA"), ("analysts", "Analyst")]
        self.auth = AuthService(
            self.repo, jwt_secret=SECRET, identity_repository=self.repo  # type: ignore[arg-type]
        )
        self.provider = OidcProvider(
            OidcConfig(ISSUER, CLIENT_ID, REDIRECT_URI, client_secret=CLIENT_SECRET,
                       attribute_map={"region": "region"}),
            transport=self.idp.transport(),
        )
        self.store = InMemoryOidcStateStore()
        self.clock = Clock()
        self.service = OidcLoginService(self.provider, self.store, self.auth, clock=self.clock)

    def sign_in(self, user: str) -> tuple[str, str, str]:
        """Run begin and the IdP leg; return (state, code, ip)."""
        request = self.service.begin("ip")
        return request.state, self.idp.authorize(request.url, user), "ip"

    def complete(self, user: str):  # type: ignore[no-untyped-def]
        state, code, ip = self.sign_in(user)
        return self.service.complete(code=code, state=state, browser_state=state, client_key=ip)


def test_login_opens_the_same_local_session_with_mapped_role_and_attributes() -> None:
    w = World()
    result = w.complete("asha")
    assert result.user.role_name == "Analyst" and result.user.attributes == {"region": "West"}
    assert result.access_token and result.refresh_token
    assert w.repo.users["asha"]["source"] == "oidc" and w.repo.users["asha"]["hash"] is None
    assert w.repo.sessions == ["id-asha"]
    assert (w.repo.audits[-1][1], w.repo.audits[-1][2]) == ("login_succeeded", {"source": "oidc"})
    # the access token is verified exactly like a password login's
    assert w.auth.authenticate_access_token(result.access_token).username == "asha"


def test_the_first_matching_mapping_wins_for_oidc_too() -> None:
    assert World().complete("ravi").user.role_name == "DBA"


def test_removing_a_group_removes_the_role_at_the_next_login() -> None:
    w = World()
    assert w.complete("ravi").user.role_name == "DBA"
    w.idp.users["ravi"]["groups"] = ["analysts"]
    assert w.complete("ravi").user.role_name == "Analyst"
    w.idp.users["ravi"]["groups"] = []
    with pytest.raises(AuthenticationError):
        w.complete("ravi")
    assert w.repo.audits[-1][2]["reason"] == "no_role_mapping"


def test_ldap_and_oidc_mappings_are_separate_sources() -> None:
    w = World()
    seen: list[str] = []
    original = w.repo.get_identity_mappings

    def spy(source: str) -> list[tuple[str, str]]:
        seen.append(source)
        return original(source)

    w.repo.get_identity_mappings = spy  # type: ignore[method-assign]
    w.complete("asha")
    assert seen == ["oidc"]


def test_a_person_with_no_mapped_group_is_refused_and_audited() -> None:
    w = World()
    with pytest.raises(AuthenticationError):
        w.complete("mina")
    assert w.repo.audits[-1][2] == {"source": "oidc", "reason": "no_role_mapping"}
    assert w.repo.sessions == [] and "mina" not in w.repo.users


def test_oidc_cannot_take_over_a_local_account() -> None:
    w = World()
    w.repo.add_local("asha")
    with pytest.raises(AuthenticationError):
        w.complete("asha")
    assert w.repo.audits[-1][2]["reason"] == "account_conflict"


def test_a_disabled_account_stays_disabled() -> None:
    w = World()
    w.complete("asha")
    w.repo.users["asha"]["status"] = "disabled"
    with pytest.raises(AuthenticationError):
        w.complete("asha")
    assert w.repo.audits[-1][2]["reason"] == "account_disabled"


def test_an_oidc_account_cannot_sign_in_with_a_password() -> None:
    w = World()
    w.complete("asha")
    with pytest.raises(AuthenticationError):
        w.auth.login("asha", "anything-at-all-123", "ip")


def test_state_is_one_time_use() -> None:
    w = World()
    state, code, ip = w.sign_in("asha")
    w.service.complete(code=code, state=state, browser_state=state, client_key=ip)
    with pytest.raises(AuthenticationError):
        w.service.complete(code=code, state=state, browser_state=state, client_key=ip)
    assert w.repo.audits[-1][2]["reason"] == "state_unknown_or_expired"


def test_state_expires() -> None:
    w = World()
    state, code, ip = w.sign_in("asha")
    w.clock.now += timedelta(seconds=w.service.ttl_seconds + 1)
    with pytest.raises(AuthenticationError):
        w.service.complete(code=code, state=state, browser_state=state, client_key=ip)
    assert w.repo.sessions == []


def test_a_failed_attempt_still_burns_the_state() -> None:
    w = World()
    state, _, ip = w.sign_in("asha")
    with pytest.raises(AuthenticationError):
        w.service.complete(code="forged", state=state, browser_state=state, client_key=ip)
    assert w.store.consume(state_hash(state), w.clock.now) is None


@pytest.mark.parametrize("browser_state", [None, "", "another-browsers-state"])
def test_the_callback_must_come_from_the_browser_that_started_the_login(browser_state: str | None) -> None:
    w = World()
    state, code, ip = w.sign_in("asha")
    with pytest.raises(AuthenticationError):
        w.service.complete(code=code, state=state, browser_state=browser_state, client_key=ip)
    assert w.repo.audits[-1][2]["reason"] == "state_cookie_mismatch" and w.repo.sessions == []


def test_unknown_state_and_missing_parameters_are_refused() -> None:
    w = World()
    _, code, ip = w.sign_in("asha")
    for state, c in (("never-issued", code), (None, code), ("x", None)):
        with pytest.raises(AuthenticationError):
            w.service.complete(code=c, state=state, browser_state=state, client_key=ip)
    assert w.repo.sessions == []


def test_an_id_token_with_the_wrong_nonce_is_refused() -> None:
    w = World()
    w.idp.token_overrides = {"nonce": "replayed-from-another-login"}
    with pytest.raises(AuthenticationError):
        w.complete("asha")
    assert w.repo.audits[-1][2]["reason"] == "token_rejected" and w.repo.sessions == []


def test_an_idp_outage_is_a_generic_failure() -> None:
    w = World()
    state, code, ip = w.sign_in("asha")
    w.idp.down = True
    with pytest.raises(AuthenticationError, match="Single sign-on failed"):
        w.service.complete(code=code, state=state, browser_state=state, client_key=ip)
    assert w.repo.audits[-1][2]["reason"] == "provider_unavailable"
    fresh = OidcProvider(w.provider._config, transport=w.idp.transport())  # nothing cached yet
    with pytest.raises(AuthenticationError, match="unavailable"):
        OidcLoginService(fresh, InMemoryOidcStateStore(), w.auth).begin("ip")


def test_pending_logins_are_capped_so_login_cannot_exhaust_memory() -> None:
    w = World()
    w.service = OidcLoginService(w.provider, InMemoryOidcStateStore(capacity=2), w.auth)
    w.service.begin("ip"), w.service.begin("ip")
    with pytest.raises(AuthenticationError):
        w.service.begin("ip")


def test_repeated_failures_from_one_address_are_rate_limited() -> None:
    w = World()
    # An address may fail 10x the per-account limit (people share one behind a proxy).
    for _ in range(50):
        with pytest.raises(AuthenticationError):
            w.service.complete(code="c", state="s", browser_state="s", client_key="9.9.9.9")
    state, code, _ = w.sign_in("asha")
    with pytest.raises(AuthenticationError):
        w.service.complete(code=code, state=state, browser_state=state, client_key="9.9.9.9")
    assert w.repo.sessions == []


# ---- HTTP routes ------------------------------------------------------------------------


def client(world: World | None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[dependencies.get_oidc_service] = lambda: world.service if world else None
    return TestClient(app, follow_redirects=False)


def test_login_redirects_to_the_provider_and_sets_a_lax_httponly_state_cookie() -> None:
    w = World()
    response = client(w).get("/api/v1/auth/oidc/login")
    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    query = parse_qs(location.query)
    assert location.netloc == "idp.example.test" and query["code_challenge_method"] == ["S256"]
    cookie = response.headers["set-cookie"].lower()
    assert f"{STATE_COOKIE}={query['state'][0]}".lower() in cookie
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/api/v1/auth/oidc" in cookie
    assert "no-store" in response.headers["cache-control"]


def test_callback_success_sets_the_refresh_cookie_and_returns_the_login_payload() -> None:
    w = World()
    c = client(w)
    login = c.get("/api/v1/auth/oidc/login")
    request_url = login.headers["location"]
    code = w.idp.authorize(request_url, "ravi")
    state = parse_qs(urlparse(request_url).query)["state"][0]
    response = c.get("/api/v1/auth/oidc/callback", params={"code": code, "state": state})
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer" and body["access_token"] and body["must_change_password"] is False
    cookies = " ".join(response.headers.get_list("set-cookie")).lower()
    assert "refresh_token=" in cookies and "httponly" in cookies
    assert w.auth.authenticate_access_token(body["access_token"]).role_name == "DBA"


def test_callback_without_the_browser_cookie_is_401() -> None:
    w = World()
    state, code, _ = w.sign_in("asha")
    response = client(w).get("/api/v1/auth/oidc/callback", params={"code": code, "state": state})
    assert response.status_code == 401 and response.json() == {"detail": "Single sign-on failed"}
    assert "refresh_token" not in response.headers.get("set-cookie", "")


def test_callback_with_a_provider_error_or_nothing_is_401() -> None:
    c = client(World())
    assert c.get("/api/v1/auth/oidc/callback", params={"error": "access_denied"}).status_code == 401
    assert c.get("/api/v1/auth/oidc/callback").status_code == 401


def test_callback_for_an_unmapped_person_is_401_not_a_session() -> None:
    w = World()
    c = client(w)
    request_url = c.get("/api/v1/auth/oidc/login").headers["location"]
    code = w.idp.authorize(request_url, "mina")
    state = parse_qs(urlparse(request_url).query)["state"][0]
    response = c.get("/api/v1/auth/oidc/callback", params={"code": code, "state": state})
    assert response.status_code == 401 and w.repo.sessions == []


def test_routes_are_404_when_oidc_is_not_configured() -> None:
    c = client(None)
    assert c.get("/api/v1/auth/oidc/login").status_code == 404
    assert c.get("/api/v1/auth/oidc/callback", params={"code": "x", "state": "y"}).status_code == 404


def test_a_different_provider_subject_cannot_take_over_an_existing_account() -> None:
    w = World()
    w.complete("asha")
    assert w.repo.users["asha"]["subject"] == "sub-asha"

    w.idp.users["asha"]["sub"] = "sub-someone-else"  # same username, a different person
    with pytest.raises(AuthenticationError):
        w.complete("asha")
    assert w.repo.audits[-1][2]["reason"] == "account_conflict"
