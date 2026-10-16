"""Regression tests for the Day 8 security review findings."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.api import governance_routes
from app.api.routes import _require_manageable_user
from app.api.security import host_is_forbidden, validate_allowed_hosts
from app.auth.models import AuthContext
from app.semantic.compiler import HARD_LIMIT, compile_query
from tests.support.ecommerce_mdl import build_mdl, build_policies, build_principal, load_cases

OVERLAY = load_cases()["model_overlay"]
MDL = build_mdl(OVERLAY)
POLICIES = build_policies(OVERLAY)


def _context(user_id: str, role: str, capabilities: set[str]) -> AuthContext:
    return AuthContext(
        user_id=user_id, username=user_id, role_name=role,
        capabilities=frozenset(capabilities), project_ids=frozenset({"p"}),
        must_change_password=False,
    )


class FakeRepo:
    def __init__(self, targets: dict[str, AuthContext], roles: dict[str, set[str]]) -> None:
        self.targets, self.roles = targets, roles

    def get_auth_context(self, user_id: str) -> AuthContext | None:
        return self.targets.get(user_id)

    def get_role_capabilities(self, name: str) -> frozenset[str] | None:
        return frozenset(self.roles[name]) if name in self.roles else None


def test_manager_cannot_change_own_access_or_a_stronger_account() -> None:
    manager = _context("11111111-1111-1111-1111-111111111111", "UserAdmin", {"manage_users"})
    dba = _context("22222222-2222-2222-2222-222222222222", "DBA", {"manage_users", "edit_mdl"})
    peer = _context("33333333-3333-3333-3333-333333333333", "Clerk", {"manage_users"})
    repo: Any = FakeRepo(
        {manager.user_id: manager, dba.user_id: dba, peer.user_id: peer},
        {"UserAdmin": {"manage_users"}, "DBA": {"manage_users", "edit_mdl"},
         "Clerk": {"manage_users"}},
    )
    with pytest.raises(HTTPException) as own:
        _require_manageable_user(manager, repo, UUID(manager.user_id))
    assert own.value.status_code == 403
    with pytest.raises(HTTPException) as stronger:
        _require_manageable_user(manager, repo, UUID(dba.user_id))
    assert stronger.value.status_code == 403
    _require_manageable_user(manager, repo, UUID(peer.user_id))  # a weaker account is fine


def test_a_written_limit_is_clamped_to_the_hard_ceiling() -> None:
    principal = build_principal("west", {"region": "West"})
    compiled = compile_query("SELECT order_id FROM orders LIMIT 2000000000", MDL, principal, POLICIES)
    assert compiled.ok
    assert f"LIMIT {HARD_LIMIT}" in compiled.sql
    assert "2000000000" not in compiled.sql
    small = compile_query("SELECT order_id FROM orders LIMIT 5", MDL, principal, POLICIES)
    assert "LIMIT 5" in small.sql


def test_ipv4_mapped_metadata_address_is_refused() -> None:
    assert host_is_forbidden("::ffff:169.254.169.254")
    assert host_is_forbidden("169.254.169.254")
    with pytest.raises(ValueError):
        validate_allowed_hosts(["::ffff:169.254.169.254"])
    with pytest.raises(ValueError):
        validate_allowed_hosts(["::ffff:169.254.0.0/112"])
    assert validate_allowed_hosts(["10.0.0.0/8", "DB.internal"]) == ["10.0.0.0/8", "db.internal"]


def test_compile_preview_sample_needs_a_preview_user_and_the_saved_model() -> None:
    project = UUID("00000000-0000-0000-0000-000000000001")
    context = AuthContext(
        user_id="u", username="u", role_name="Editor", capabilities=frozenset({"edit_mdl"}),
        project_ids=frozenset({str(project)}), must_change_password=False,
    )
    unused: Any = SimpleNamespace()
    for payload in (
        {"logicalSql": "SELECT order_id FROM orders", "sample": True},
        {"logicalSql": "SELECT order_id FROM orders", "sample": True,
         "previewAsUserId": "00000000-0000-0000-0000-000000000002",
         "mdl": MDL.model_dump(mode="json", by_alias=True)},
    ):
        body = governance_routes.CompilePreviewRequest.model_validate(payload)
        with pytest.raises(HTTPException) as caught:
            governance_routes.compile_preview(project, body, context, unused, unused, unused)
        assert caught.value.status_code == 422
