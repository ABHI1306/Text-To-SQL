"""Day 4 governance: prompt context, policy validation and storage, audit browsing, previews."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import dependencies
from app.api.governance_routes import router as governance_router
from app.audit.service import AuditService
from app.auth.models import AuthContext
from app.core.types import AuditEvent
from app.domain.policy import RowPolicy
from app.policy.service import PolicyNotFound, PolicyRejected, PolicyService
from app.policy.validation import validate_policy
from app.ports.audit import AuditFilter, AuditPage, AuditRecord
from app.ports.policy_repo import StoredPolicy
from app.semantic.prompt_context import render_prompt_context
from tests.support.audit import RecordingAudit
from tests.support.ecommerce_mdl import build_mdl, build_policies, build_principal

PROJECT_ID = "00000000-0000-0000-0000-0000000000c1"
DBA_ID = "00000000-0000-0000-0000-0000000000d1"
TEST_USER_ID = "00000000-0000-0000-0000-0000000000d2"
KNOWN = {"region", "regions"}


def policy(**overrides: Any) -> RowPolicy:
    values: dict[str, Any] = {
        "name": "orders_region",
        "model": "orders",
        "predicate": "region = {{ user.region }}",
    }
    return RowPolicy.model_validate({**values, **overrides})


# --------------------------------------------------------------- prompt context


def test_prompt_context_uses_logical_names_and_hides_physical_tables() -> None:
    text = render_prompt_context(build_mdl())
    assert "CREATE TABLE orders (" in text
    assert "ecommerce." not in text
    assert "order_items" in text and "line_total NUMERIC" in text
    assert "calculated: quantity * unit_price" in text
    assert "Relationship orders_customers: orders to customers (MANY_TO_ONE)" in text
    assert "Metric monthly_revenue (over payments)" in text
    assert "measure revenue = sum(amount)" in text


def test_prompt_context_limits_to_selected_models_and_their_relationships() -> None:
    text = render_prompt_context(build_mdl(), model_names=["orders", "customers"])
    assert "CREATE TABLE payments" not in text
    assert "orders_customers" in text
    assert "order_items_orders" not in text
    assert "monthly_revenue" not in text


# ------------------------------------------------------------ policy validation


def test_valid_policy_compiles_for_the_test_user() -> None:
    result = validate_policy(policy(), build_mdl(), build_principal("u", {"region": "EU"}), KNOWN)
    assert result.ok and not result.warnings
    assert result.test_compilation is not None
    assert result.test_compilation.parameters == {"p_region": "EU"}


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"model": "ghosts"}, "not in the semantic model"),
        ({"predicate": "region = {{ user.country }}"}, "unknown user attribute"),
        ({"predicate": "ghost = {{ user.region }}"}, "unknown column"),
        ({"predicate": "region = {{ user.region "}, "placeholders"),
        ({"predicate": "region = = {{ user.region }}"}, "does not parse"),
        ({"predicate": "order_id IN (SELECT order_id FROM payments)"}, "subqueries"),
        ({"predicate": "customers.region = {{ user.region }}"}, "unknown column"),
    ],
)
def test_invalid_policies_are_rejected_with_a_reason(overrides: dict[str, Any], fragment: str) -> None:
    result = validate_policy(
        policy(**overrides), build_mdl(), build_principal("u", {"region": "EU"}), KNOWN | {"x"}
    )
    assert not result.ok
    assert any(fragment in error.message for error in result.errors)


def test_role_scope_cannot_hide_a_broken_predicate_from_validation() -> None:
    scoped = policy(predicate="ghost = 1", appliesTo={"roles": ["somebody_else"]})
    result = validate_policy(scoped, build_mdl(), build_principal("u", {}, role="user"), KNOWN)
    assert not result.ok


def test_missing_attribute_for_the_test_user_is_a_warning_not_an_error() -> None:
    result = validate_policy(policy(), build_mdl(), build_principal("u", {}), KNOWN)
    assert result.ok
    assert "denies every row" in result.warnings[0]


# -------------------------------------------------------------- policy service


class FakePolicyRepo:
    def __init__(self) -> None:
        self.rows: dict[str, StoredPolicy] = {}
        self.audit: list[AuditEvent] = []

    def list_policies(self, project_id: str, *, enabled_only: bool = False) -> list[StoredPolicy]:
        return [r for r in self.rows.values() if r.project_id == project_id and (r.enabled or not enabled_only)]

    def get_policy(self, project_id: str, policy_id: str) -> StoredPolicy | None:
        return self.rows.get(policy_id)

    def create_policy(self, project_id, row_policy, *, enabled, actor_id, audit):  # type: ignore[no-untyped-def]
        stored = StoredPolicy(str(uuid4()), project_id, row_policy, enabled, actor_id, datetime.now(UTC))
        self.rows[stored.id] = stored
        self.audit.append(audit)
        return stored

    def update_policy(self, project_id, policy_id, row_policy, *, enabled, actor_id, audit):  # type: ignore[no-untyped-def]
        if policy_id not in self.rows:
            return None
        stored = StoredPolicy(policy_id, project_id, row_policy, enabled, actor_id, datetime.now(UTC))
        self.rows[policy_id] = stored
        self.audit.append(audit)
        return stored

    def delete_policy(self, project_id: str, policy_id: str, *, audit: AuditEvent) -> bool:
        if self.rows.pop(policy_id, None) is None:
            return False
        self.audit.append(audit)
        return True

    def known_attribute_keys(self) -> frozenset[str]:
        return frozenset(KNOWN)


def service() -> tuple[PolicyService, FakePolicyRepo]:
    repo = FakePolicyRepo()
    return PolicyService(repo), repo


def create(svc: PolicyService, row_policy: RowPolicy | None = None, attributes: dict[str, Any] | None = None):  # type: ignore[no-untyped-def]
    return svc.create(
        PROJECT_ID,
        row_policy or policy(),
        enabled=True,
        mdl=build_mdl(),
        test_principal=build_principal("t", attributes if attributes is not None else {"region": "EU"}),
        actor_id=DBA_ID,
    )


def test_creating_a_policy_stores_it_and_writes_one_audit_event() -> None:
    svc, repo = service()
    saved = create(svc)
    assert repo.rows[saved.stored.id].policy.name == "orders_region"
    (event,) = repo.audit
    assert event.action == "row_policy_created" and event.actor_id == DBA_ID
    assert event.details["predicate"] == "region = {{ user.region }}"
    assert event.details["test_user_id"] == "t"


def test_an_invalid_policy_is_neither_stored_nor_audited() -> None:
    svc, repo = service()
    with pytest.raises(PolicyRejected):
        create(svc, policy(predicate="ghost = {{ user.region }}"))
    assert repo.rows == {} and repo.audit == []


def test_updating_records_the_previous_predicate() -> None:
    svc, repo = service()
    stored = create(svc).stored
    svc.update(
        PROJECT_ID,
        stored.id,
        policy(predicate="region <> {{ user.region }}"),
        enabled=False,
        mdl=build_mdl(),
        test_principal=build_principal("t", {"region": "EU"}),
        actor_id=DBA_ID,
    )
    event = repo.audit[-1]
    assert event.action == "row_policy_updated"
    assert event.details["previous"]["predicate"] == "region = {{ user.region }}"  # type: ignore[index]
    assert event.details["enabled"] is False


def test_deleting_audits_and_unknown_ids_raise() -> None:
    svc, repo = service()
    stored = create(svc).stored
    svc.delete(PROJECT_ID, stored.id, actor_id=DBA_ID)
    assert repo.audit[-1].action == "row_policy_deleted"
    with pytest.raises(PolicyNotFound):
        svc.delete(PROJECT_ID, stored.id, actor_id=DBA_ID)


# ----------------------------------------------------------------- audit service


class FakeAuditPort:
    def __init__(self) -> None:
        self.appended: list[AuditEvent] = []
        self.queries: list[tuple[AuditFilter, int, int | None]] = []

    def append(self, event: AuditEvent) -> None:
        self.appended.append(event)

    def query(self, filters: AuditFilter, *, limit: int, before_id: int | None = None) -> AuditPage:
        self.queries.append((filters, limit, before_id))
        record = AuditRecord(7, DBA_ID, "mdl_saved", "project", PROJECT_ID, {"revision": 2}, datetime.now(UTC))
        return AuditPage((record,), next_cursor=7)


def test_audit_service_records_events_and_clamps_page_size() -> None:
    port = FakeAuditPort()
    svc = AuditService(port)
    svc.record(DBA_ID, "mdl_saved", "project", PROJECT_ID, {"revision": 2})
    assert port.appended[0].details == {"revision": 2}
    svc.browse(AuditFilter(), limit=10_000)
    assert port.queries[0][1] == 200
    with pytest.raises(ValueError, match="earlier"):
        now = datetime.now(UTC)
        svc.browse(AuditFilter(since=now, until=now))


def test_audit_port_has_no_update_or_delete() -> None:
    assert not any(hasattr(AuditService, name) for name in ("update", "delete", "remove", "clear"))


# ---------------------------------------------------------------------- API


class FakeRepository:
    """Metadata repository double for the governance routes."""

    def __init__(self) -> None:
        self.mdl = {**build_mdl().model_dump(by_alias=True, mode="json"), "project_id": PROJECT_ID, "revision": 1}

    def get_mdl(self, project_id: str, version: int | None = None) -> dict[str, object] | None:
        return self.mdl

    def get_active_mdl_version(self, project_id: str) -> int | None:
        return None

    def get_auth_context(self, user_id: str) -> AuthContext | None:
        if user_id != TEST_USER_ID:
            return None
        return AuthContext(
            user_id=TEST_USER_ID,
            username="eu",
            role_name="User",
            capabilities=frozenset(),
            project_ids=frozenset({PROJECT_ID}),
            must_change_password=False,
            attributes={"region": "EU"},
        )


def client(capabilities: set[str], policies: FakePolicyRepo | None = None, audit_port: FakeAuditPort | None = None):  # type: ignore[no-untyped-def]
    context = AuthContext(
        user_id=DBA_ID,
        username="dba",
        role_name="DBA",
        capabilities=frozenset(capabilities),
        project_ids=frozenset({PROJECT_ID}),
        must_change_password=False,
    )
    recorder = RecordingAudit()
    policies = policies or FakePolicyRepo()
    app = FastAPI()
    app.include_router(governance_router)
    app.dependency_overrides[dependencies.get_current_context] = lambda: context
    app.dependency_overrides[dependencies.get_repository] = lambda: FakeRepository()
    app.dependency_overrides[dependencies.get_governance_repository] = lambda: policies
    app.dependency_overrides[dependencies.get_audit_service] = lambda: (
        AuditService(audit_port) if audit_port else recorder
    )
    return TestClient(app, raise_server_exceptions=False), policies, recorder


BASE = f"/api/v1/projects/{PROJECT_ID}"


def test_compile_preview_returns_compiled_sql_without_policies_by_default() -> None:
    test_client, _, recorder = client({"edit_mdl"})
    response = test_client.post(
        f"{BASE}/compile-preview", json={"logicalSql": "SELECT status FROM orders"}
    )
    body = response.json()
    assert response.status_code == 200 and body["ok"]
    assert "FROM ecommerce.orders" in body["sql"] and body["parameters"] == {}
    assert body["preview"] == {"asUserId": None, "labeled": False}
    assert recorder.events == []


def test_compile_errors_are_returned_inline_with_suggestions() -> None:
    test_client, _, _ = client({"edit_mdl"})
    body = test_client.post(
        f"{BASE}/compile-preview", json={"logicalSql": "SELECT order_dt FROM orders"}
    ).json()
    assert body["ok"] is False and body["sql"] == ""
    assert body["errors"][0]["code"] == "UNKNOWN_COLUMN"
    assert "ordered_at" in body["errors"][0]["suggestions"]


def test_compile_preview_checks_an_unsaved_draft_model() -> None:
    draft = build_mdl().model_dump(by_alias=True, mode="json")
    draft["models"][4]["columns"].append(
        {"name": "age_days", "type": "integer", "isCalculated": True,
         "expression": "extract(day from now() - ordered_at)", "description": "age"}
    )
    test_client, _, _ = client({"edit_mdl"})
    body = test_client.post(
        f"{BASE}/compile-preview", json={"logicalSql": "SELECT age_days FROM orders", "mdl": draft}
    ).json()
    assert body["ok"], body["errors"]
    broken = {**draft}
    broken["models"][4]["columns"][-1]["expression"] = "ghost_column + 1"
    assert test_client.post(
        f"{BASE}/compile-preview", json={"logicalSql": "SELECT 1 FROM orders", "mdl": broken}
    ).status_code == 422


def test_preview_as_user_applies_policies_is_labeled_and_audited() -> None:
    policies = FakePolicyRepo()
    for row_policy in build_policies()[:1]:
        policies.rows["p1"] = StoredPolicy("p1", PROJECT_ID, row_policy, True, DBA_ID, datetime.now(UTC))
    test_client, _, recorder = client({"edit_mdl"}, policies)
    body = test_client.post(
        f"{BASE}/compile-preview",
        json={"logicalSql": "SELECT order_id FROM orders", "previewAsUserId": TEST_USER_ID},
    ).json()
    assert body["parameters"] == {"p_region": "EU"}
    assert body["preview"] == {"asUserId": TEST_USER_ID, "labeled": True}
    assert body["appliedPolicies"] == ["orders_region"]
    (event,) = recorder.events
    assert event["action"] == "preview_as_user" and event["details"]["as_user_id"] == TEST_USER_ID


def test_preview_as_an_unknown_user_is_404() -> None:
    test_client, _, _ = client({"edit_mdl"})
    response = test_client.post(
        f"{BASE}/compile-preview",
        json={"logicalSql": "SELECT 1 FROM orders", "previewAsUserId": str(uuid4())},
    )
    assert response.status_code == 404


def policy_body(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "orders_region",
        "model": "orders",
        "predicate": "region = {{ user.region }}",
        "testUserId": TEST_USER_ID,
        **overrides,
    }


def test_policy_crud_validates_and_audits() -> None:
    test_client, repo, _ = client({"manage_policies"})
    created = test_client.post(f"{BASE}/row-policies", json=policy_body())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["validation"]["testCompilation"]["parameters"] == {"p_region": "EU"}
    policy_id = body["id"]
    listed = test_client.get(f"{BASE}/row-policies").json()["items"]
    assert [item["name"] for item in listed] == ["orders_region"]
    updated = test_client.put(
        f"{BASE}/row-policies/{policy_id}", json=policy_body(predicate="region <> {{ user.region }}")
    )
    assert updated.status_code == 200
    assert test_client.delete(f"{BASE}/row-policies/{policy_id}").status_code == 204
    assert [event.action for event in repo.audit] == [
        "row_policy_created",
        "row_policy_updated",
        "row_policy_deleted",
    ]
    assert test_client.delete(f"{BASE}/row-policies/{policy_id}").status_code == 404


def test_invalid_policy_returns_typed_errors_and_stores_nothing() -> None:
    test_client, repo, _ = client({"manage_policies"})
    response = test_client.post(
        f"{BASE}/row-policies", json=policy_body(predicate="ghost = {{ user.region }}")
    )
    assert response.status_code == 422
    assert response.json()["detail"]["errors"][0]["code"] == "POLICY_ERROR"
    assert repo.rows == {} and repo.audit == []


def test_policy_with_an_unknown_test_user_is_404() -> None:
    test_client, repo, _ = client({"manage_policies"})
    response = test_client.post(f"{BASE}/row-policies", json=policy_body(testUserId=str(uuid4())))
    assert response.status_code == 404 and repo.rows == {}


def test_policy_routes_require_manage_policies() -> None:
    test_client, _, _ = client({"edit_mdl", "view_audit"})
    assert test_client.get(f"{BASE}/row-policies").status_code == 403
    assert test_client.post(f"{BASE}/row-policies", json=policy_body()).status_code == 403


def test_audit_browser_filters_pages_and_requires_view_audit() -> None:
    port = FakeAuditPort()
    test_client, _, _ = client({"view_audit"}, audit_port=port)
    response = test_client.get(
        "/api/v1/audit",
        params={"action": "mdl_saved", "actor_id": DBA_ID, "limit": 25, "cursor": 40,
                "since": "2026-01-01T00:00:00Z"},
    )
    body = response.json()
    assert response.status_code == 200 and body["nextCursor"] == 7
    assert body["items"][0]["action"] == "mdl_saved"
    filters, limit, before = port.queries[0]
    assert (filters.action, filters.actor_id, limit, before) == ("mdl_saved", DBA_ID, 25, 40)
    assert filters.since is not None
    denied, _, _ = client({"edit_mdl"}, audit_port=port)
    assert denied.get("/api/v1/audit").status_code == 403


def test_audit_browser_rejects_an_inverted_time_range() -> None:
    test_client, _, _ = client({"view_audit"}, audit_port=FakeAuditPort())
    response = test_client.get(
        "/api/v1/audit", params={"since": "2026-02-01T00:00:00Z", "until": "2026-01-01T00:00:00Z"}
    )
    assert response.status_code == 422


# ------------------------------------------------------------------ deploy report


def test_deploy_report_counts_coverage_and_checks_expressions() -> None:
    from app.semantic.deploy_report import build_deploy_report

    document = build_mdl().model_dump(by_alias=True, mode="json")
    document["models"][0]["description"] = ""
    document["models"][1]["columns"][0]["description"] = ""
    report = build_deploy_report(type(build_mdl()).model_validate(document))
    assert report["models"] == {"described": 7, "total": 8, "percent": 88}
    assert report["deployable"] is True
    reviewed = {item["model"] for item in report["needsReview"]}  # type: ignore[attr-defined]
    assert {"customers", "addresses"} <= reviewed
    kinds = {check["kind"] for check in report["checks"]}  # type: ignore[attr-defined]
    assert kinds == {"calculated_field", "metric", "relationship"}


def test_deploy_report_flags_a_broken_metric_as_not_deployable() -> None:
    from app.semantic.deploy_report import build_deploy_report

    mdl = build_mdl(
        {
            "metrics": [
                {
                    "name": "broken",
                    "base_model": "payments",
                    "dimensions": [],
                    "measures": [{"name": "x", "expression": "sum(amount)"}],
                }
            ]
        }
    )
    assert build_deploy_report(mdl)["deployable"] is True
    object.__setattr__(mdl.metrics[0], "base_object", "ghosts")
    report = build_deploy_report(mdl)
    assert report["deployable"] is False
    failing = [c for c in report["checks"] if not c["ok"]]  # type: ignore[attr-defined]
    assert failing[0]["name"] == "broken" and failing[0]["errors"][0]["code"] == "INVALID_MODEL"


def test_deploy_report_route_requires_deploy_mdl() -> None:
    test_client, _, _ = client({"edit_mdl"})
    assert test_client.get(f"{BASE}/deploy-report").status_code == 403
    allowed, _, _ = client({"deploy_mdl"})
    body = allowed.get(f"{BASE}/deploy-report").json()
    assert body["revision"] == 1 and body["deployable"] is True
