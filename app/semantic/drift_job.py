"""The scheduled profiling job: snapshot the live catalog, compare it with the deployed MDL."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol, cast

from app.core.catalog import CatalogTableProfile
from app.domain.mdl import Mdl
from app.ports.catalog import CatalogPort
from app.ports.drift import DriftRepoPort
from app.scheduler.runner import JobContext, JobSkipped
from app.semantic.drift import DriftResult, detect_drift


class MdlSource(Protocol):
    """Read access to a project's deployed MDL documents."""

    def get_active_mdl_version(self, project_id: str) -> int | None:
        """Deployed MDL version of the project, or None."""
        ...

    def get_mdl(
        self, project_id: str, version: int | None = None
    ) -> dict[str, object] | None:
        """MDL document of ``version`` (the latest when omitted), or None."""
        ...


def load_mdl(document: Mapping[str, object]) -> Mdl:
    """Validate a stored MDL document, ignoring its storage-only keys."""
    return Mdl.model_validate(
        {key: value for key, value in document.items() if key not in {"project_id", "revision"}}
    )


def snapshot_live(catalog: CatalogPort, mdl: Mdl) -> dict[str, CatalogTableProfile | None]:
    """Profile (without samples) every table the MDL maps; None for those that no longer exist."""
    references = sorted({model.table_reference for model in mdl.models})
    parsed = {ref: _split(ref) for ref in references}
    schemas = tuple(sorted({schema for schema, _ in parsed.values()}))
    available = {(t.schema_name, t.table_name) for t in catalog.list_tables(schemas)}
    present = tuple(pair for pair in parsed.values() if pair in available)
    profiles = catalog.profile_tables(present, sample_limit=0) if present else ()
    by_pair = {(p.table.schema_name, p.table.table_name): p for p in profiles}
    return {ref: by_pair.get(pair) for ref, pair in parsed.items()}


def _split(reference: str) -> tuple[str, str]:
    """Split ``schema.table`` into its parts, defaulting the schema to ``public``."""
    schema, _, table = reference.rpartition(".")
    return (schema or "public", table)


def compare_with_deployed(
    source: MdlSource, catalog: CatalogPort, project_id: str
) -> tuple[int, DriftResult] | None:
    """(deployed revision, drift); None when the project has nothing deployed yet."""
    version = source.get_active_mdl_version(project_id)
    if version is None:
        return None
    document = source.get_mdl(project_id, version)
    if document is None:
        return None
    mdl = load_mdl(document)
    return version, detect_drift(mdl, snapshot_live(catalog, mdl))


class DriftJobHandler:
    """Handler registered for the 'profile_drift' job kind; runs with the owner's context."""

    def __init__(
        self,
        source: MdlSource,
        catalog_for: Callable[[str], CatalogPort],
        reports: DriftRepoPort,
        notify: Callable[[str, str, dict[str, object]], None] | None = None,
    ) -> None:
        """Wire the MDL source, per-project catalog factory, report store and notifier."""
        self._source = source
        self._catalog_for = catalog_for
        self._reports = reports
        self._notify = notify

    def __call__(self, context: JobContext) -> Mapping[str, object]:
        """Compare the deployed MDL with the live catalog, save a report and notify on drift."""
        project_id = context.job.project_id
        compared = compare_with_deployed(self._source, self._catalog_for(project_id), project_id)
        if compared is None:
            raise JobSkipped("the project has no deployed MDL to compare against")
        version, result = compared
        report_id = self._reports.save_report(
            project_id=project_id,
            mdl_version=version,
            job_id=context.job.id,
            slot=context.slot,
            has_drift=result.has_drift,
            summary=result.summary,
            findings=[finding.as_json() for finding in result.findings],
            created_by=context.owner.user_id,
        )
        if result.has_drift and self._notify is not None:
            self._notify(
                context.owner.user_id,
                "drift_report",
                {"project_id": project_id, "report_id": report_id, "mdl_version": version,
                 "summary": cast(dict[str, object], result.summary)},
            )
        return {
            "reportId": report_id,
            "mdlVersion": version,
            "hasDrift": result.has_drift,
            "summary": cast(dict[str, object], result.summary),
        }
