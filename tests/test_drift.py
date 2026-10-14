"""Drift detection, drafts built from it, the scheduled job handler and the new routes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import dependencies
from app.api.drift_routes import (
    get_drift_repository,
    get_job_store,
)
from app.api.drift_routes import router as drift_router
from app.auth.models import AuthContext
from app.auth.service import TokenError
from app.core.catalog import CatalogColumn, CatalogTable, CatalogTableProfile
from app.domain.mdl import Mdl
from app.ports.drift import StoredDriftReport
from app.ports.scheduler import ScheduledJob
from app.scheduler.runner import JobContext, JobSkipped
from app.semantic.drift import DriftApplyError, DriftFinding, apply_drift, detect_drift
from app.semantic.drift_job import DriftJobHandler, snapshot_live
from tests.support.fake_scheduler import FakeJobStore


def mdl(extra_models: list[dict[str, Any]] | None = None, **overrides: Any) -> Mdl:
    document: dict[str, Any] = {
        "models": [
            {
                "name": "orders", "tableReference": "shop.orders", "primaryKey": "order_id",
                "description": "Orders",
                "columns": [
                    {"name": "order_id", "type": "integer", "isPrimaryKey": True, "description": "id"},
                    {"name": "status", "type": "text", "description": "state"},
                    {"name": "total", "type": "numeric", "physicalName": "total_amount",
                     "description": "total"},
                    {"name": "label", "type": "text", "isCalculated": True,
                     "expression": "status", "description": "calc"},
                ],
            },
            {
                "name": "customers", "tableReference": "shop.customers", "description": "Customers",
                "columns": [{"name": "customer_id", "type": "integer", "description": "id"}],
            },
            *(extra_models or []),
        ],
        "relationships": [{
            "name": "orders_customers", "models": ["orders", "customers"],
            "joinType": "MANY_TO_ONE", "condition": "orders.order_id = customers.customer_id",
        }],
    }
    document.update(overrides)
    return Mdl.model_validate(document)


def profile(table: str, **columns: str) -> CatalogTableProfile:
    schema, name = table.split(".")
    return CatalogTableProfile(
        CatalogTable(schema, name, "BASE TABLE"),
        tuple(CatalogColumn(c, t, True, c.endswith("_id") and i == 0)
              for i, (c, t) in enumerate(columns.items())),
        (), 0, (),
    )


def live(**overrides: CatalogTableProfile | None) -> dict[str, CatalogTableProfile | None]:
    base: dict[str, CatalogTableProfile | None] = {
        "shop.orders": profile("shop.orders", order_id="int4", status="text",
                               total_amount="numeric"),
        "shop.customers": profile("shop.customers", customer_id="integer"),
    }
    base.update({k.replace("__", "."): v for k, v in overrides.items()})
    return base


# ---- detection --------------------------------------------------------------------------


def test_identical_schemas_report_no_drift() -> None:
    result = detect_drift(mdl(), live())  # int4 vs integer are compatible spellings
    assert not result.has_drift and result.findings == ()
    assert result.summary == {
        "table_removed": 0, "column_added": 0, "column_removed": 0, "type_changed": 0,
    }


def test_an_added_column_is_reported_with_its_type() -> None:
    orders = profile("shop.orders", order_id="int4", status="text", total_amount="numeric",
                     coupon_code="text")
    result = detect_drift(mdl(), live(shop__orders=orders))
    assert [f.as_json() for f in result.findings] == [
        {"kind": "column_added", "table": "shop.orders", "model": "orders",
         "column": "coupon_code", "actual": "text"}
    ]
    assert result.summary["column_added"] == 1 and result.has_drift


def test_a_removed_column_is_found_through_its_physical_name() -> None:
    orders = profile("shop.orders", order_id="int4", status="text")  # total_amount is gone
    (finding,) = detect_drift(mdl(), live(shop__orders=orders)).findings
    assert (finding.kind, finding.column, finding.model, finding.expected) == (
        "column_removed", "total_amount", "orders", "numeric")


def test_an_incompatible_type_change_is_reported_but_synonyms_are_not() -> None:
    orders = profile("shop.orders", order_id="int4", status="integer", total_amount="numeric")
    (finding,) = detect_drift(mdl(), live(shop__orders=orders)).findings
    assert (finding.kind, finding.column, finding.expected, finding.actual) == (
        "type_changed", "status", "text", "integer")


def test_a_dropped_table_is_reported_once_not_per_column() -> None:
    (finding,) = detect_drift(mdl(), live(shop__customers=None)).findings
    assert (finding.kind, finding.table, finding.model, finding.column) == (
        "table_removed", "shop.customers", "customers", None)


def test_calculated_columns_are_never_expected_in_the_database() -> None:
    assert not detect_drift(mdl(), live()).has_drift  # 'label' has no physical column


def test_all_kinds_together() -> None:
    orders = profile("shop.orders", order_id="int4", status="uuid", fresh="date")
    result = detect_drift(mdl(), live(shop__orders=orders, shop__customers=None))
    assert sorted(f.kind for f in result.findings) == [
        "column_added", "column_removed", "table_removed", "type_changed"]


def test_findings_round_trip_through_json() -> None:
    finding = DriftFinding("column_added", "shop.orders", "orders", "x", actual="text",
                           is_primary_key=True)
    assert DriftFinding.from_json(finding.as_json()) == finding
    with pytest.raises(ValueError):
        DriftFinding.from_json({"kind": "nope", "table": "t", "model": "m"})


# ---- drafts -----------------------------------------------------------------------------


def test_applying_drift_follows_the_live_schema_without_touching_the_original() -> None:
    original = mdl()
    orders = profile("shop.orders", order_id="int4", status="uuid", Coupon_Code="text")
    findings = detect_drift(original, live(shop__orders=orders)).findings
    # total_amount removed would break nothing calculated; status retyped; a column added
    draft = apply_drift(original, findings)
    cols = {c.name: c for c in draft.models[0].columns}
    assert "total" not in cols
    assert cols["status"].type == "uuid"
    added = cols["coupon_code"]
    assert added.physical_name == "Coupon_Code" and added.needs_review and added.type == "text"
    assert {c.name for c in original.models[0].columns} >= {"total", "status"}
    assert original.models[0].columns[1].type == "text"


def test_a_removed_primary_key_column_clears_the_key() -> None:
    orders = profile("shop.orders", status="text", total_amount="numeric")
    base = mdl(relationships=[])
    draft = apply_drift(base, detect_drift(base, live(shop__orders=orders)).findings)
    assert draft.models[0].primary_key is None


def test_a_dropped_table_removes_its_model_relationships_and_metrics() -> None:
    base = mdl(metrics=[{
        "name": "customer_count", "baseObject": "customers",
        "measures": [{"name": "n", "expression": "count(customer_id)"}],
    }])
    draft = apply_drift(base, detect_drift(base, live(shop__customers=None)).findings)
    assert [m.name for m in draft.models] == ["orders"]
    assert draft.relationships == [] and draft.metrics == []


def test_drift_that_breaks_a_calculated_column_is_refused_not_half_applied() -> None:
    orders = profile("shop.orders", order_id="int4", total_amount="numeric")  # status dropped
    findings = detect_drift(mdl(), live(shop__orders=orders)).findings
    with pytest.raises(DriftApplyError, match="cannot be applied automatically"):
        apply_drift(mdl(), findings)


# ---- the scheduled job ------------------------------------------------------------------


class FakeCatalog:
    def __init__(self, profiles: dict[tuple[str, str], CatalogTableProfile]) -> None:
        self.profiles = profiles
        self.profiled: tuple[tuple[str, str], ...] = ()

    def list_tables(self, schemas: tuple[str, ...] = ()) -> tuple[CatalogTable, ...]:
        return tuple(p.table for key, p in self.profiles.items() if not schemas or key[0] in schemas)

    def profile_tables(
        self, tables: tuple[tuple[str, str], ...], *, sample_limit: int = 3
    ) -> tuple[CatalogTableProfile, ...]:
        assert sample_limit == 0, "drift detection must not read row samples"
        self.profiled = tables
        return tuple(self.profiles[t] for t in tables)


class FakeSource:
    def __init__(self, document: Mdl | None, version: int | None = 4) -> None:
        self.document, self.version = document, version

    def get_active_mdl_version(self, project_id: str) -> int | None:
        return self.version

    def get_mdl(self, project_id: str, version: int | None = None) -> dict[str, object] | None:
        if self.document is None:
            return None
        return {**self.document.model_dump(by_alias=True, mode="json"),
                "project_id": project_id, "revision": version}


class FakeReports:
    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []
        self.reports: dict[str, StoredDriftReport] = {}
        self.drafts: list[dict[str, object]] = []

    def save_report(self, **kwargs: Any) -> str:
        for existing in self.saved:
            if kwargs["job_id"] and (existing["job_id"], existing["slot"]) == (
                    kwargs["job_id"], kwargs["slot"]):
                return str(existing["id"])
        report_id = str(uuid4())
        self.saved.append({**kwargs, "id": report_id})
        self.reports[report_id] = StoredDriftReport(
            report_id, kwargs["project_id"], kwargs["mdl_version"], kwargs["has_drift"],
            kwargs["summary"], kwargs["findings"], None, kwargs["created_by"],
            datetime.now(UTC) - timedelta(minutes=len(self.saved)), kwargs["job_id"],
        )
        return report_id

    def list_reports(self, project_id: str, *, limit: int, before: datetime | None = None
                     ) -> list[StoredDriftReport]:
        items = [r for r in self.reports.values() if r.project_id == project_id]
        items = [r for r in items if before is None or r.created_at < before]
        return sorted(items, key=lambda r: r.created_at, reverse=True)[:limit]

    def get_report(self, project_id: str, report_id: str) -> StoredDriftReport | None:
        report = self.reports.get(report_id)
        return report if report and report.project_id == project_id else None

    def create_draft(self, project_id: str, report_id: str, document: dict[str, object], *,
                     created_by: str) -> int | None:
        report = self.reports[report_id]
        if report.draft_mdl_version is not None:
            return None
        self.drafts.append(document)
        revision = 4 + len(self.drafts)
        self.reports[report_id] = StoredDriftReport(**{**report.__dict__, "draft_mdl_version": revision})
        return revision


def job_context() -> JobContext:
    store = FakeJobStore()
    job = ScheduledJob("j1", "p1", "owner", "profile_drift", "0 * * * *", "UTC", "active", store.now)
    owner = AuthContext("owner", "o", "DBA", frozenset({"manage_profiling"}), frozenset({"p1"}), False)
    return JobContext(job, store.now, 1, owner)


def catalog_for(**tables: dict[str, str]) -> FakeCatalog:
    return FakeCatalog({
        (name.split("__")[0], name.split("__")[1]): profile(name.replace("__", "."), **cols)
        for name, cols in tables.items()
    })


def test_the_job_stores_a_no_drift_report_when_nothing_changed() -> None:
    reports = FakeReports()
    catalog = catalog_for(
        shop__orders={"order_id": "int4", "status": "text", "total_amount": "numeric"},
        shop__customers={"customer_id": "integer"},
    )
    result = DriftJobHandler(FakeSource(mdl()), lambda _: catalog, reports)(job_context())
    assert result["hasDrift"] is False and result["mdlVersion"] == 4
    saved = reports.saved[0]
    assert saved["has_drift"] is False and saved["findings"] == []
    assert saved["created_by"] == "owner" and saved["job_id"] == "j1"
    assert set(catalog.profiled) == {("shop", "orders"), ("shop", "customers")}


def test_the_job_stores_findings_for_a_drifted_catalog() -> None:
    reports = FakeReports()
    catalog = catalog_for(  # customers table dropped, orders gained a column
        shop__orders={"order_id": "int4", "status": "text", "total_amount": "numeric", "x": "date"},
    )
    result = DriftJobHandler(FakeSource(mdl()), lambda _: catalog, reports)(job_context())
    assert result["hasDrift"] is True
    assert result["summary"] == {
        "table_removed": 1, "column_added": 1, "column_removed": 0, "type_changed": 0}
    assert catalog.profiled == (("shop", "orders"),)  # a missing table is never profiled


def test_the_same_slot_never_stores_two_reports() -> None:
    reports = FakeReports()
    catalog = catalog_for(shop__orders={"order_id": "int4", "status": "text", "total_amount": "numeric"},
                          shop__customers={"customer_id": "integer"})
    handler = DriftJobHandler(FakeSource(mdl()), lambda _: catalog, reports)
    context = job_context()
    assert handler(context)["reportId"] == handler(context)["reportId"]
    assert len(reports.saved) == 1


def test_a_project_without_a_deployed_mdl_is_skipped() -> None:
    handler = DriftJobHandler(FakeSource(None, version=None), lambda _: catalog_for(), FakeReports())
    with pytest.raises(JobSkipped):
        handler(job_context())


def test_snapshot_reports_missing_tables_as_none() -> None:
    snapshot = snapshot_live(catalog_for(shop__orders={"order_id": "int4"}), mdl())
    assert snapshot["shop.customers"] is None and snapshot["shop.orders"] is not None


# ---- routes -----------------------------------------------------------------------------

PROJECT = "11111111-1111-1111-1111-111111111111"
OTHER_PROJECT = "22222222-2222-2222-2222-222222222222"


class FakeMdlRepo:
    def __init__(self, document: Mdl | None) -> None:
        self.document = document

    def get_mdl(self, project_id: str, version: int | None = None) -> dict[str, object] | None:
        return FakeSource(self.document).get_mdl(project_id, version)


def client(capabilities: set[str], *, projects: frozenset[str] = frozenset({PROJECT}),
           authenticated: bool = True, document: Mdl | None = None,
           ) -> tuple[TestClient, FakeReports, FakeJobStore, MagicMock]:
    reports, jobs, audit = FakeReports(), FakeJobStore(), MagicMock()
    context = AuthContext("u1", "dba", "DBA", frozenset(capabilities), projects, False)
    app = FastAPI()
    app.include_router(drift_router)
    if authenticated:
        app.dependency_overrides[dependencies.get_current_context] = lambda: context
    else:
        auth = MagicMock()
        auth.authenticate_access_token.side_effect = TokenError("bad")
        app.dependency_overrides[dependencies.get_auth_service] = lambda: auth
    app.dependency_overrides[get_drift_repository] = lambda: reports
    app.dependency_overrides[get_job_store] = lambda: jobs
    app.dependency_overrides[dependencies.get_repository] = lambda: FakeMdlRepo(document or mdl())
    app.dependency_overrides[dependencies.get_audit_service] = lambda: audit
    return TestClient(app), reports, jobs, audit


def seed(reports: FakeReports, findings: list[dict[str, object]], *, project: str = PROJECT) -> str:
    return reports.save_report(
        project_id=project, mdl_version=4, job_id="j1", slot=datetime.now(UTC),
        has_drift=bool(findings), summary={"column_added": len(findings)}, findings=findings,
        created_by="owner",
    )


ADDED = {"kind": "column_added", "table": "shop.orders", "model": "orders",
         "column": "coupon_code", "actual": "text"}

ROUTES: list[tuple[str, str, Callable[[str], dict[str, Any]] | None]] = []


def test_drift_report_list_succeeds_for_a_profiling_manager() -> None:
    c, reports, _, _ = client({"manage_profiling"})
    seed(reports, [ADDED])
    seed(reports, [], project=OTHER_PROJECT)
    response = c.get(f"/api/v1/projects/{PROJECT}/drift-reports")
    assert response.status_code == 200
    (item,) = response.json()["items"]
    assert item["hasDrift"] is True and item["findings"] == [ADDED] and item["mdlVersion"] == 4
    assert response.json()["nextCursor"] is None


def test_drift_report_listing_pages_by_created_at() -> None:
    c, reports, _, _ = client({"manage_profiling"})
    for _ in range(3):
        seed(reports, [])
        reports.saved[-1]["slot"] = None
    first = c.get(f"/api/v1/projects/{PROJECT}/drift-reports", params={"limit": 2}).json()
    assert len(first["items"]) == 2 and first["nextCursor"]
    second = c.get(f"/api/v1/projects/{PROJECT}/drift-reports",
                   params={"limit": 2, "cursor": first["nextCursor"]}).json()
    assert len(second["items"]) == 1 and second["nextCursor"] is None


def test_drift_routes_require_authentication() -> None:
    c, *_ = client(set(), authenticated=False)
    report = "33333333-3333-3333-3333-333333333333"
    for method, path in (
        ("GET", f"/api/v1/projects/{PROJECT}/drift-reports"),
        ("POST", f"/api/v1/projects/{PROJECT}/drift-reports/{report}/draft"),
        ("GET", f"/api/v1/projects/{PROJECT}/profiling-schedule"),
        ("PUT", f"/api/v1/projects/{PROJECT}/profiling-schedule"),
        ("DELETE", f"/api/v1/projects/{PROJECT}/profiling-schedule"),
    ):
        response = c.request(method, path, json={"schedule": "0 * * * *"} if method == "PUT" else None)
        assert response.status_code == 401, (method, path)
        response = c.request(method, path, headers={"Authorization": "Bearer nope"},
                             json={"schedule": "0 * * * *"} if method == "PUT" else None)
        assert response.status_code == 401, (method, path)


def test_drift_routes_require_their_capability() -> None:
    c, reports, _, _ = client({"ask_question", "edit_mdl"})  # no manage_profiling
    report = seed(reports, [ADDED])
    assert c.get(f"/api/v1/projects/{PROJECT}/drift-reports").status_code == 403
    assert c.get(f"/api/v1/projects/{PROJECT}/profiling-schedule").status_code == 403
    assert c.put(f"/api/v1/projects/{PROJECT}/profiling-schedule",
                 json={"schedule": "0 * * * *"}).status_code == 403
    assert c.delete(f"/api/v1/projects/{PROJECT}/profiling-schedule").status_code == 403
    c2, reports2, _, _ = client({"manage_profiling"})  # no edit_mdl
    report2 = seed(reports2, [ADDED])
    assert c2.post(f"/api/v1/projects/{PROJECT}/drift-reports/{report2}/draft").status_code == 403
    assert report and reports.drafts == [] and reports2.drafts == []


def test_a_project_the_caller_cannot_see_is_404() -> None:
    c, *_ = client({"manage_profiling", "edit_mdl"}, projects=frozenset())
    assert c.get(f"/api/v1/projects/{PROJECT}/drift-reports").status_code == 404
    assert c.post(f"/api/v1/projects/{PROJECT}/drift-reports/{PROJECT}/draft").status_code == 404
    assert c.put(f"/api/v1/projects/{PROJECT}/profiling-schedule",
                 json={"schedule": "0 * * * *"}).status_code == 404


def test_draft_from_drift_saves_a_new_revision_and_only_once() -> None:
    c, reports, _, _ = client({"edit_mdl"})
    report = seed(reports, [ADDED])
    response = c.post(f"/api/v1/projects/{PROJECT}/drift-reports/{report}/draft")
    assert response.status_code == 201
    body = response.json()
    assert body["revision"] == 5 and body["baseRevision"] == 4 and body["status"] == "draft"
    saved_columns = {col["name"] for col in reports.drafts[0]["models"][0]["columns"]}  # type: ignore[index]
    assert "coupon_code" in saved_columns
    again = c.post(f"/api/v1/projects/{PROJECT}/drift-reports/{report}/draft")
    assert again.status_code == 409 and "already" in again.json()["detail"]


def test_draft_edge_cases_are_clear_errors() -> None:
    c, reports, _, _ = client({"edit_mdl"})
    clean = seed(reports, [])
    assert c.post(f"/api/v1/projects/{PROJECT}/drift-reports/{clean}/draft").status_code == 409
    missing = "44444444-4444-4444-4444-444444444444"
    assert c.post(f"/api/v1/projects/{PROJECT}/drift-reports/{missing}/draft").status_code == 404
    other = seed(reports, [ADDED], project=OTHER_PROJECT)  # another project's report
    assert c.post(f"/api/v1/projects/{PROJECT}/drift-reports/{other}/draft").status_code == 404
    breaking = seed(reports, [{"kind": "column_removed", "table": "shop.orders",
                               "model": "orders", "column": "status", "expected": "text"}])
    response = c.post(f"/api/v1/projects/{PROJECT}/drift-reports/{breaking}/draft")
    assert response.status_code == 409 and "cannot be applied" in response.json()["detail"]
    assert reports.drafts == []


def test_schedule_set_get_delete_with_audit_and_owner() -> None:
    c, _, jobs, audit = client({"manage_profiling"})
    url = f"/api/v1/projects/{PROJECT}/profiling-schedule"
    assert c.get(url).status_code == 404
    response = c.put(url, json={"schedule": "0  3 * * *", "timezone": "Asia/Kolkata"})
    assert response.status_code == 200
    body = response.json()
    assert body["schedule"] == "0 3 * * *" and body["ownerId"] == "u1" and body["status"] == "active"
    assert jobs.get_for_project(PROJECT, "profile_drift") is not None
    assert audit.record.call_args.args[:2] == ("u1", "profiling_schedule_set")
    assert c.get(url).json()["timezone"] == "Asia/Kolkata"
    assert c.delete(url).status_code == 204 and c.delete(url).status_code == 404
    assert any(call.args[1] == "profiling_schedule_removed" for call in audit.record.call_args_list)


@pytest.mark.parametrize("body", [
    {"schedule": "* * * * *"}, {"schedule": "not a cron"}, {"schedule": "0 3 * * *", "timezone": "Mars/X"},
    {"schedule": "0 3 * * *", "extra": 1}, {},
])
def test_invalid_schedules_are_422_and_store_nothing(body: dict[str, Any]) -> None:
    c, _, jobs, audit = client({"manage_profiling"})
    assert c.put(f"/api/v1/projects/{PROJECT}/profiling-schedule", json=body).status_code == 422
    assert jobs.jobs == {} and not audit.record.called
