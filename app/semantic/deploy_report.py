"""Deploy report: description coverage and compiler checks of a semantic model (D4-C3)."""

from __future__ import annotations

from app.domain.mdl import Mdl
from app.semantic.compiler import compile_query


def _share(part: int, total: int) -> dict[str, object]:
    """Described/total counts with a percentage (100 when there is nothing to describe)."""
    return {
        "described": part,
        "total": total,
        "percent": round(100 * part / total) if total else 100,
    }


def build_deploy_report(mdl: Mdl) -> dict[str, object]:
    """Coverage and compile status. Compile failures block deployment; gaps only flag review."""
    models_described = sum(1 for model in mdl.models if model.description.strip())
    columns = [(model.name, column) for model in mdl.models for column in model.columns]
    columns_described = sum(1 for _, column in columns if column.description.strip())
    needs_review = []
    for model in mdl.models:
        undescribed = [c.name for c in model.columns if not c.description.strip()]
        if model.needs_review or undescribed:
            needs_review.append(
                {
                    "model": model.name,
                    "modelDescribed": bool(model.description.strip()),
                    "undescribedColumns": undescribed,
                }
            )
    checks: list[dict[str, object]] = []
    for model in mdl.models:
        for column in model.columns:
            if column.is_calculated:
                checks.append(
                    _check(
                        "calculated_field",
                        f"{model.name}.{column.name}",
                        f"SELECT {column.name} FROM {model.name}",
                        mdl,
                    )
                )
    for metric in mdl.metrics:
        checks.append(_check("metric", metric.name, f"SELECT * FROM {metric.name}", mdl))
    for relationship in mdl.relationships:
        left, right = relationship.models
        checks.append(
            _check(
                "relationship",
                relationship.name,
                f"SELECT 1 FROM {left} JOIN {right} ON {relationship.condition}",
                mdl,
            )
        )
    return {
        "models": _share(models_described, len(mdl.models)),
        "columns": _share(columns_described, len(columns)),
        "needsReview": needs_review,
        "checks": checks,
        "deployable": all(check["ok"] for check in checks),
    }


def _check(kind: str, name: str, sql: str, mdl: Mdl) -> dict[str, object]:
    """Compile ``sql`` structurally (no principal) and report whether it passed."""
    result = compile_query(sql, mdl, None)
    return {
        "kind": kind,
        "name": name,
        "ok": result.ok,
        "errors": [
            {"code": error.code.value, "message": error.message} for error in result.errors
        ],
    }
