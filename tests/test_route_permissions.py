"""Route-walk and generated 403 tests (FR-20) plus the role matrix (FR-19)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api import dependencies
from app.api.ask_routes import router as ask_router
from app.api.governance_routes import router as governance_router
from app.api.routes import _CAPABILITIES, router
from app.auth.models import AuthContext

# Routes that never need a bearer token.
PUBLIC_ROUTES = {
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/refresh"),
    ("POST", "/api/v1/auth/logout"),
}
# Routes open to any signed-in account; each filters its data by the caller.
AUTHENTICATED_ROUTES = {
    ("POST", "/api/v1/auth/change-password"),
    ("GET", "/api/v1/me"),
    ("GET", "/api/v1/projects"),
}

USER_CAPABILITIES = frozenset({"ask_question", "give_feedback", "view_shared"})
ANALYST_CAPABILITIES = frozenset(
    {
        "ask_question",
        "give_feedback",
        "view_shared",
        "create_dashboard",
        "schedule_question",
        "share",
    }
)
ROLE_MATRIX = {
    "DBA": frozenset(_CAPABILITIES),
    "User": USER_CAPABILITIES,
    "Analyst+": ANALYST_CAPABILITIES,
}


@dataclass(frozen=True)
class RouteInfo:
    method: str
    path: str
    capability: str | None
    route: APIRoute


def _dependency_calls(dependant: Any) -> Iterator[Any]:
    for sub in dependant.dependencies:
        yield sub.call
        yield from _dependency_calls(sub)


def _routes() -> list[RouteInfo]:
    found: list[RouteInfo] = []
    for route in [*router.routes, *governance_router.routes, *ask_router.routes]:
        assert isinstance(route, APIRoute)
        capabilities = {
            call.required_capability
            for call in _dependency_calls(route.dependant)
            if hasattr(call, "required_capability")
        }
        assert len(capabilities) <= 1, f"{route.path} declares several capabilities"
        for method in sorted(route.methods):
            found.append(
                RouteInfo(method, route.path, next(iter(capabilities), None), route)
            )
    return found


ROUTES = _routes()
GUARDED = [info for info in ROUTES if info.capability is not None]


def test_every_route_declares_a_capability_or_is_explicitly_exempt() -> None:
    exempt = PUBLIC_ROUTES | AUTHENTICATED_ROUTES
    undeclared = {
        (info.method, info.path) for info in ROUTES if info.capability is None
    }
    assert undeclared == exempt, (
        "Routes without a capability must be listed as public or authenticated-only: "
        f"unexpected={sorted(undeclared - exempt)} stale={sorted(exempt - undeclared)}"
    )


def test_declared_capabilities_exist_in_the_capability_catalogue() -> None:
    unknown = {info.capability for info in GUARDED} - set(_CAPABILITIES)
    assert not unknown


def test_the_catalogue_has_the_19_documented_capabilities() -> None:
    assert len(_CAPABILITIES) == 19


def _context(role: str, capabilities: frozenset[str]) -> AuthContext:
    return AuthContext(
        user_id="00000000-0000-0000-0000-0000000000aa",
        username=role.lower(),
        role_name=role,
        capabilities=capabilities,
        project_ids=frozenset(),
        must_change_password=False,
    )


def _client(context: AuthContext) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.include_router(governance_router)
    app.include_router(ask_router)
    app.dependency_overrides[dependencies.get_current_context] = lambda: context
    app.dependency_overrides[dependencies.get_repository] = lambda: MagicMock()
    return TestClient(app, raise_server_exceptions=False)


def _call(client: TestClient, info: RouteInfo) -> int:
    path = info.path
    for name in info.route.param_convertors:
        path = path.replace("{" + name + "}", "00000000-0000-0000-0000-0000000000bb")
    path = path.replace("{template_id}", "ecommerce")
    return client.request(info.method, path, json={}).status_code


@pytest.mark.parametrize("role", sorted(ROLE_MATRIX))
def test_role_matrix_denies_exactly_the_routes_whose_capability_is_missing(
    role: str,
) -> None:
    capabilities = ROLE_MATRIX[role]
    client = _client(_context(role, capabilities))
    wrong: list[str] = []
    for info in GUARDED:
        denied = _call(client, info) == 403
        should_deny = info.capability not in capabilities
        if denied != should_deny:
            wrong.append(f"{info.method} {info.path} needs {info.capability}: denied={denied}")
    assert not wrong, "\n".join(wrong)


@pytest.mark.parametrize("info", GUARDED, ids=lambda i: f"{i.method} {i.path}")
def test_user_without_the_capability_gets_403_on_every_guarded_route(
    info: RouteInfo,
) -> None:
    holding_nothing = _context("Nobody", frozenset())
    assert _call(_client(holding_nothing), info) == 403


@pytest.mark.parametrize("info", GUARDED, ids=lambda i: f"{i.method} {i.path}")
def test_holding_only_the_required_capability_is_not_denied(info: RouteInfo) -> None:
    assert info.capability is not None
    context = _context("Single", frozenset({info.capability}))
    assert _call(_client(context), info) != 403


def test_dba_holds_every_capability_the_routes_need() -> None:
    assert {info.capability for info in GUARDED} <= ROLE_MATRIX["DBA"]


def test_plain_user_cannot_read_the_semantic_model() -> None:
    info = next(i for i in ROUTES if i.method == "GET" and i.path.endswith("/mdl"))
    assert info.capability == "edit_mdl"
    assert _call(_client(_context("User", USER_CAPABILITIES)), info) == 403
