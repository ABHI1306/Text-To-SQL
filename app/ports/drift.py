"""Port for storing schema-drift reports and the draft models made from them."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class StoredDriftReport:
    """A persisted drift report: findings against one MDL version and any resulting draft."""
    id: str
    project_id: str
    mdl_version: int
    has_drift: bool
    summary: dict[str, int]
    findings: list[dict[str, object]]
    draft_mdl_version: int | None
    created_by: str | None
    created_at: datetime
    job_id: str | None = None


class DriftRepoPort(Protocol):
    """Storage for drift reports, scoped to a project."""
    def save_report(
        self,
        *,
        project_id: str,
        mdl_version: int,
        job_id: str | None,
        slot: datetime | None,
        has_drift: bool,
        summary: dict[str, int],
        findings: list[dict[str, object]],
        created_by: str | None,
    ) -> str:
        """Store a report and return its id; the same (job, slot) stores only one."""
        ...

    def list_reports(
        self, project_id: str, *, limit: int, before: datetime | None = None
    ) -> list[StoredDriftReport]:
        """Return up to ``limit`` reports, newest first, created before ``before`` when given."""
        ...

    def get_report(self, project_id: str, report_id: str) -> StoredDriftReport | None:
        """Return the report of this project, or None when missing or in another project."""
        ...

    def create_draft(
        self,
        project_id: str,
        report_id: str,
        document: dict[str, object],
        *,
        created_by: str,
    ) -> int | None:
        """Save ``document`` as a new draft MDL revision and link it to the report, atomically.

        Returns the revision, or None when the report already produced a draft.
        """
        ...
