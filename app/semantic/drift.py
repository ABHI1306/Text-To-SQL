"""Schema drift: how the live catalog differs from the deployed MDL, and a draft that follows it.

Detection compares only what the MDL maps (its models' tables and their non-calculated
columns) against a catalog snapshot, so tables nobody modelled are not "drift". Applying the
findings to the MDL never touches descriptions, metrics or relationships that stay valid; an
MDL that cannot be made consistent (a calculated column or metric still uses a dropped
column) is refused with a message instead of being saved half-fixed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from app.core.catalog import CatalogTableProfile
from app.domain.mdl import Mdl, MdlColumn, MdlValidationError, _compatible_type
from app.semantic.profiling import _safe_name, _unique_name

FindingKind = Literal["column_added", "column_removed", "type_changed", "table_removed"]


@dataclass(frozen=True)
class DriftFinding:
    """One difference between a mapped table or column and the live catalog."""
    kind: FindingKind
    table: str  # schema.table, as in the MDL's tableReference
    model: str
    column: str | None = None  # physical column name
    expected: str | None = None  # type recorded in the MDL
    actual: str | None = None  # type found in the database
    is_primary_key: bool = False

    def as_json(self) -> dict[str, object]:
        """JSON form of the finding, omitting unset fields."""
        data: dict[str, object] = {"kind": self.kind, "table": self.table, "model": self.model}
        for key in ("column", "expected", "actual"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        if self.is_primary_key:
            data["isPrimaryKey"] = True
        return data

    @staticmethod
    def from_json(data: Mapping[str, object]) -> DriftFinding:
        """Rebuild a finding from its JSON form, rejecting unknown kinds."""
        kind = str(data["kind"])
        if kind not in ("column_added", "column_removed", "type_changed", "table_removed"):
            raise ValueError(f"Unknown drift finding {kind!r}")

        def text(key: str) -> str | None:
            """Value of ``key`` as a string, or None when absent."""
            value = data.get(key)
            return str(value) if value is not None else None

        return DriftFinding(
            kind=kind,  # type: ignore[arg-type]
            table=str(data["table"]),
            model=str(data["model"]),
            column=text("column"),
            expected=text("expected"),
            actual=text("actual"),
            is_primary_key=bool(data.get("isPrimaryKey", False)),
        )


@dataclass(frozen=True)
class DriftResult:
    """All drift findings of one comparison."""
    findings: tuple[DriftFinding, ...]

    @property
    def has_drift(self) -> bool:
        """True when any finding exists."""
        return bool(self.findings)

    @property
    def summary(self) -> dict[str, int]:
        """Finding counts per kind, with zeros for kinds that did not occur."""
        counts = Counter(finding.kind for finding in self.findings)
        return {
            kind: counts.get(kind, 0)
            for kind in ("table_removed", "column_added", "column_removed", "type_changed")
        }


def detect_drift(mdl: Mdl, live: Mapping[str, CatalogTableProfile | None]) -> DriftResult:
    """Compare the MDL with ``live`` (table reference -> profile, or None when it is gone)."""
    findings: list[DriftFinding] = []
    for model in mdl.models:
        profile = live.get(model.table_reference)
        if profile is None:
            findings.append(DriftFinding("table_removed", model.table_reference, model.name))
            continue
        actual = {column.name: column for column in profile.columns}
        mapped: dict[str, MdlColumn] = {
            column.physical_name or column.name: column
            for column in model.columns
            if not column.is_calculated
        }
        for physical, column in mapped.items():
            found = actual.get(physical)
            if found is None:
                findings.append(DriftFinding(
                    "column_removed", model.table_reference, model.name, physical,
                    expected=column.type,
                ))
            elif not _compatible_type(column.type, found.data_type):
                findings.append(DriftFinding(
                    "type_changed", model.table_reference, model.name, physical,
                    expected=column.type, actual=found.data_type,
                ))
        for physical, found in actual.items():
            if physical not in mapped:
                findings.append(DriftFinding(
                    "column_added", model.table_reference, model.name, physical,
                    actual=found.data_type, is_primary_key=found.is_primary_key,
                ))
    return DriftResult(tuple(findings))


class DriftApplyError(ValueError):
    """The findings cannot be applied without producing an invalid semantic model."""


def apply_drift(mdl: Mdl, findings: tuple[DriftFinding, ...]) -> Mdl:
    """A new MDL that follows the live schema; ``mdl`` is left untouched."""
    draft = mdl.model_copy(deep=True)
    removed_models: set[str] = set()
    for finding in findings:
        model = next((m for m in draft.models if m.name == finding.model), None)
        if model is None:
            continue
        if finding.kind == "table_removed":
            removed_models.add(model.name)
            continue
        if finding.column is None:
            raise DriftApplyError(f"Finding for {finding.model} has no column")
        physical = finding.column
        existing = next(
            (c for c in model.columns
             if not c.is_calculated and (c.physical_name or c.name) == physical),
            None,
        )
        if finding.kind == "column_removed" and existing is not None:
            model.columns.remove(existing)
            if model.primary_key == existing.name:
                model.primary_key = None
        elif finding.kind == "type_changed" and existing is not None and finding.actual:
            existing.type = finding.actual
            existing.needs_review = True
        elif finding.kind == "column_added" and existing is None and finding.actual:
            used = {c.name for c in model.columns}
            logical = _unique_name(_safe_name(physical), used)
            model.columns.append(MdlColumn(
                name=logical,
                type=finding.actual,
                physicalName=physical if logical != physical else None,
                isPrimaryKey=finding.is_primary_key and model.primary_key is None,
                needsReview=True,
            ))
            if finding.is_primary_key and model.primary_key is None:
                model.primary_key = logical
    if removed_models:
        draft.models = [m for m in draft.models if m.name not in removed_models]
        draft.relationships = [
            r for r in draft.relationships if not set(r.models) & removed_models
        ]
        draft.metrics = [m for m in draft.metrics if m.base_object not in removed_models]
    try:
        # Re-run every semantic validator against the edited document.
        return Mdl.model_validate(draft.model_dump(by_alias=True, mode="json"))
    except (ValidationError, MdlValidationError, ValueError) as error:
        raise DriftApplyError(
            "The drift cannot be applied automatically because the model still depends on "
            f"removed columns or tables: {_first_line(error)}"
        ) from error


def _first_line(error: Exception) -> str:
    """Short readable detail of a validation error, skipping the generic header line."""
    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    return " ".join(lines[1:3]) if len(lines) > 1 else (lines[0] if lines else "invalid model")
