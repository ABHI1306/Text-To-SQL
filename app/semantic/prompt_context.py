"""Render logical models as compact DDL-style prompt context.

The LLM sees logical names only: physical table references, physical column names and row
policies never appear in the output.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.domain.mdl import Mdl, MdlColumn, MdlModel, Metric, Relationship


def render_prompt_context(
    mdl: Mdl,
    *,
    model_names: Sequence[str] | None = None,
    include_instructions: bool = True,
) -> str:
    """DDL-style text for ``model_names`` (all models when omitted) and what connects them."""
    selected = set(model_names) if model_names is not None else {m.name for m in mdl.models}
    models = [model for model in mdl.models if model.name in selected]
    blocks = [_render_model(model) for model in models]
    relationships = [
        relationship
        for relationship in mdl.relationships
        if set(relationship.models) <= selected
    ]
    if relationships:
        blocks.append("\n".join(_render_relationship(r) for r in relationships))
    metrics = [metric for metric in mdl.metrics if metric.base_object in selected]
    blocks.extend(_render_metric(metric) for metric in metrics)
    if include_instructions and mdl.instructions:
        blocks.append("\n".join(f"-- Instruction: {text}" for text in mdl.instructions))
    return "\n\n".join(blocks)


def _render_model(model: MdlModel) -> str:
    lines: list[str] = []
    if model.description.strip():
        lines.append(f"-- {_one_line(model.description)}")
    rendered = [_render_column(model, column) for column in model.columns]
    body = ",\n".join(f"  {line}" for line in rendered)
    lines.append(f"CREATE TABLE {model.name} (\n{body}\n);")
    return "\n".join(lines)


def _render_column(model: MdlModel, column: MdlColumn) -> str:
    parts = [f"{column.name} {column.type.upper()}"]
    if column.is_primary_key or model.primary_key == column.name:
        parts.append("PRIMARY KEY")
    if column.is_calculated:
        parts.append(f"/* calculated: {_one_line(column.expression or '')} */")
    if column.description.strip():
        parts.append(f"-- {_one_line(column.description)}")
    return " ".join(parts)


def _render_relationship(relationship: Relationship) -> str:
    left, right = relationship.models
    return (
        f"-- Relationship {relationship.name}: {left} to {right} "
        f"({relationship.join_type.value}) ON {_one_line(relationship.condition)}"
    )


def _render_metric(metric: Metric) -> str:
    lines = [f"-- Metric {metric.name} (over {metric.base_object}), usable like a table:"]
    if metric.description.strip():
        lines.append(f"--   {_one_line(metric.description)}")
    lines.extend(
        f"--   dimension {field.name} = {_one_line(field.expression)}" for field in metric.dimensions
    )
    lines.extend(
        f"--   measure {field.name} = {_one_line(field.expression)}" for field in metric.measures
    )
    return "\n".join(lines)


def _one_line(text: str) -> str:
    return " ".join(text.split())
