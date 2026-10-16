"""LDAP login through AuthService: provisioning, group-to-role mapping and refusal paths."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from app.auth.identity import resolve_role
from app.auth.models import AuthContext, AuthUser
from app.auth.service import AuthenticationError, AuthService, hash_password
from app.core.types import JsonValue
from app.ports.directory import DirectoryIdentity, DirectoryUnavailable

SECRET = "x" * 40


class FakeDirectory:
    def __init__(self) -> None:
        self.people: dict[str, tuple[str, DirectoryIdentity]] = {}
        self.down = False
        self.calls: list[str] = []

    def add(self, username: str, password: str, groups: tuple[str, ...], **attributes: JsonValue) -> None:
        self.people[username] = (password, DirectoryIdentity(username, f"uid={username}", groups, attributes))

    def authenticate(self, username: str, password: str) -> DirectoryIdentity | None:
        self.calls.append(username)
        if self.down:
            raise DirectoryUnavailable("down")
        person = self.people.get(username)
        return person[1] if person and person[0] == password and password else None


class Repo:
    """Minimal AuthRepository + IdentityRepository double with real provisioning semantics."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.mappings: list[tuple[str, str]] = []
        self.audits: list[tuple[str | None, str, dict[str, object]]] = []
        self.sessions: list[str] = []
        self.failures: list[str] = []

    def add_local(self, username: str) -> None:
        self.users[username] = {"id": f"id-{username}", "role": "DBA", "source": "local",
                                "hash": hash_password("a-long-local-password"), "attrs": {}, "status": "active"}

    # AuthRepository
    def get_user_by_username(self, username: str) -> AuthUser | None:
        user = self.users.get(username)
        if not user:
            return None
        return AuthUser(user["id"], username, user["role"], user["hash"], user["status"], False, None, user["source"])

    def get_auth_context(self, user_id: str) -> AuthContext | None:
        for name, user in self.users.items():
            if user["id"] == user_id:
                return AuthContext(user_id, name, user["role"], frozenset({"ask_question"}), frozenset(), False,
                                   attributes=dict(user["attrs"]))
        return None

    def record_login_failure(self, user_id: str, **_: Any) -> None:
        self.failures.append(user_id)

    def clear_login_failures(self, user_id: str) -> None:
        pass

    def session_is_alive(self, session_id: str, now: Any) -> bool:
        return True

    def create_session(self, **kwargs: Any) -> None:
        self.sessions.append(kwargs["user_id"])

    def append_auth_audit(self, actor_id: str | None, action: str, username: str, details: dict[str, object]) -> None:
        self.audits.append((actor_id, action, details))

    # IdentityRepository
    def get_identity_mappings(self, source: str) -> list[tuple[str, str]]:
        return list(self.mappings)

    def provision_external_user(self, username: str, source: str, role_name: str,
                                attributes: Mapping[str, JsonValue], *,
                                subject: str | None = None) -> str:
        existing = self.users.get(username)
        if existing and existing["source"] != source:
            raise ValueError("another source")
        if existing and subject is not None and existing.get("subject") not in (None, subject):
            raise ValueError("different identity")
        if existing is None:
            self.users[username] = {"id": f"id-{username}", "role": role_name, "source": source,
                                    "hash": None, "attrs": {}, "status": "active"}
        user = self.users[username]
        if subject is not None:
            user["subject"] = subject
        user["role"] = role_name
        user["attrs"] = dict(attributes)
        return str(user["id"])


def service(directory: FakeDirectory | None, repo: Repo) -> AuthService:
    return AuthService(repo, jwt_secret=SECRET, directory=directory, identity_repository=repo)  # type: ignore[arg-type]


def setup() -> tuple[FakeDirectory, Repo, AuthService]:
    directory, repo = FakeDirectory(), Repo()
    directory.add("asha", "asha-pw", ("analysts",), region="West")
    directory.add("ravi", "ravi-pw", ("dbas", "analysts"))
    directory.add("mina", "mina-pw", ("interns",))
    repo.mappings = [("dbas", "DBA"), ("analysts", "Analyst")]
    return directory, repo, service(directory, repo)


def test_first_login_creates_the_account_with_the_mapped_role_and_attributes() -> None:
    _, repo, auth = setup()
    result = auth.login("asha", "asha-pw", "ip")
    assert result.user.role_name == "Analyst" and result.user.attributes == {"region": "West"}
    assert repo.users["asha"]["source"] == "ldap" and repo.users["asha"]["hash"] is None
    assert repo.sessions == ["id-asha"]
    assert (repo.audits[-1][1], repo.audits[-1][2]) == ("login_succeeded", {"source": "ldap"})


def test_the_first_matching_mapping_wins_in_creation_order() -> None:
    _, _, auth = setup()
    assert auth.login("ravi", "ravi-pw", "ip").user.role_name == "DBA"


def test_removing_a_group_removes_the_role_at_the_next_login() -> None:
    directory, repo, auth = setup()
    assert auth.login("ravi", "ravi-pw", "ip").user.role_name == "DBA"
    directory.add("ravi", "ravi-pw", ("analysts",))
    assert auth.login("ravi", "ravi-pw", "ip").user.role_name == "Analyst"
    directory.add("ravi", "ravi-pw", ())
    with pytest.raises(AuthenticationError):
        auth.login("ravi", "ravi-pw", "ip")
    assert repo.audits[-1][2]["reason"] == "no_role_mapping"


@pytest.mark.parametrize(("user", "password", "reason"), [
    ("asha", "wrong", "invalid_credentials"),
    ("ghost", "x", "invalid_credentials"),
    ("asha", "", "invalid_credentials"),
    ("mina", "mina-pw", "no_role_mapping"),
])
def test_refusals_look_identical_to_the_caller_and_are_audited_with_a_reason(user: str, password: str, reason: str) -> None:
    _, repo, auth = setup()
    with pytest.raises(AuthenticationError, match="Invalid username or password"):
        auth.login(user, password, "ip")
    assert repo.audits[-1][1] == "login_failed" and repo.audits[-1][2]["reason"] == reason
    assert repo.sessions == []


def test_a_directory_outage_is_a_generic_failure_and_never_falls_back_to_a_password() -> None:
    directory, repo, auth = setup()
    directory.down = True
    with pytest.raises(AuthenticationError, match="Invalid username or password"):
        auth.login("asha", "asha-pw", "ip")
    assert repo.audits[-1][2]["reason"] == "directory_unavailable" and repo.sessions == []


def test_local_accounts_never_reach_the_directory_and_keep_working() -> None:
    directory, repo, auth = setup()
    repo.add_local("localdba")
    directory.add("localdba", "dir-password", ("dbas",))
    assert auth.login("localdba", "a-long-local-password", "ip").user.username == "localdba"
    with pytest.raises(AuthenticationError):  # the directory password must not open a local account
        auth.login("localdba", "dir-password", "ip")
    assert directory.calls == []


def test_a_directory_user_cannot_take_over_an_existing_local_name() -> None:
    directory, repo, _ = setup()
    repo.add_local("asha")
    auth = service(directory, repo)
    with pytest.raises(AuthenticationError):
        auth.login("asha", "asha-pw", "ip")
    assert directory.calls == []  # local account: the directory is not consulted at all


def test_a_disabled_directory_account_stays_disabled() -> None:
    _, repo, auth = setup()
    auth.login("asha", "asha-pw", "ip")
    repo.users["asha"]["status"] = "disabled"
    with pytest.raises(AuthenticationError):
        auth.login("asha", "asha-pw", "ip")
    assert repo.audits[-1][2]["reason"] == "account_disabled"


def test_without_a_directory_unknown_users_are_simply_refused() -> None:
    repo = Repo()
    with pytest.raises(AuthenticationError):
        service(None, repo).login("asha", "asha-pw", "ip")
    assert repo.audits[-1][2] == {"source": "local"}


def test_resolve_role_matches_groups_case_insensitively_in_mapping_order() -> None:
    mappings = [("CN=dbas,OU=g", "DBA"), ("analysts", "Analyst")]
    assert resolve_role(["cn=dbas,ou=g"], mappings) == "DBA"
    assert resolve_role(["Analysts", "x"], mappings) == "Analyst"
    assert resolve_role(["nobody"], mappings) is None
    assert resolve_role([], mappings) is None
