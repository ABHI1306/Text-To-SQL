"""Pure checks for what the small model says about a result: chart specs and summaries.

Neither is trusted. A chart spec is rebuilt from an allow-list and every field it names must be a
real result column; a summary may only quote numbers that are in the rows. Anything else yields
nothing, and the caller shows the rows without it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

VEGA_LITE_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"
MARKS = frozenset({"bar", "line", "point", "area", "arc", "circle", "square", "tick"})
CHANNELS = frozenset({"x", "y", "color", "theta", "size"})
TYPES = frozenset({"quantitative", "nominal", "ordinal", "temporal"})
AGGREGATES = frozenset({"sum", "mean", "count", "min", "max", "median"})
_TOP_LEVEL = frozenset({"$schema", "title", "description", "mark", "encoding", "width", "height"})
_CHANNEL_KEYS = frozenset({"field", "type", "aggregate", "title"})
MAX_TITLE = 120


@dataclass(frozen=True)
class ChartValidation:
    spec: dict[str, Any] | None
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.spec is not None


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def validate_chart_spec(
    spec: object, columns: Sequence[str], rows: Sequence[Sequence[object]]
) -> ChartValidation:
    """A sanitized Vega-Lite spec bound to the dataset named ``rows``, or the reasons it failed."""
    errors: list[str] = []
    if not isinstance(spec, Mapping):
        return ChartValidation(None, ("the chart is not an object",))
    if not rows:
        return ChartValidation(None, ("there are no rows to chart",))
    for key in spec:
        if key not in _TOP_LEVEL:
            errors.append(f"unknown chart key: {key}")
    schema = spec.get("$schema")
    if schema is not None and schema != VEGA_LITE_SCHEMA:
        errors.append("unexpected $schema")

    mark = spec.get("mark")
    mark_type = mark.get("type") if isinstance(mark, Mapping) else mark
    if isinstance(mark, Mapping) and set(mark) - {"type"}:
        errors.append("mark options are not allowed")
    if not isinstance(mark_type, str) or mark_type not in MARKS:
        errors.append(f"invalid mark: {mark_type!r}")

    encoding = spec.get("encoding")
    clean_encoding: dict[str, dict[str, str]] = {}
    if not isinstance(encoding, Mapping) or not encoding:
        errors.append("the chart has no encoding")
    else:
        index = {name: position for position, name in enumerate(columns)}
        for channel, definition in encoding.items():
            if channel not in CHANNELS:
                errors.append(f"unknown channel: {channel}")
                continue
            clean, problems = _channel(channel, definition, index, rows)
            errors.extend(problems)
            if clean is not None:
                clean_encoding[channel] = clean
    if not errors and not ({"x", "y", "theta"} & set(clean_encoding)):
        errors.append("the chart needs an x, y or theta channel")

    for key in ("title", "description"):
        value = spec.get(key)
        if value is not None and not isinstance(value, str):
            errors.append(f"{key} must be text")
    for key in ("width", "height"):
        value = spec.get(key)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or not 50 <= value <= 2000
        ):
            errors.append(f"{key} is out of range")
    if errors:
        return ChartValidation(None, tuple(errors))

    clean_spec: dict[str, Any] = {
        "$schema": VEGA_LITE_SCHEMA,
        "data": {"name": "rows"},
        "mark": mark_type,
        "encoding": clean_encoding,
    }
    for key in ("title", "description"):
        if isinstance(spec.get(key), str):
            clean_spec[key] = str(spec[key])[:MAX_TITLE]
    for key in ("width", "height"):
        if spec.get(key) is not None:
            clean_spec[key] = spec[key]
    return ChartValidation(clean_spec)


def _channel(
    channel: str,
    definition: object,
    index: Mapping[str, int],
    rows: Sequence[Sequence[object]],
) -> tuple[dict[str, str] | None, list[str]]:
    if not isinstance(definition, Mapping):
        return None, [f"channel {channel} is not an object"]
    errors = [f"unknown key in {channel}: {key}" for key in definition if key not in _CHANNEL_KEYS]
    aggregate = definition.get("aggregate")
    field_name = definition.get("field")
    kind = definition.get("type")
    if aggregate is not None and aggregate not in AGGREGATES:
        errors.append(f"invalid aggregate in {channel}: {aggregate!r}")
    if kind not in TYPES:
        errors.append(f"invalid type in {channel}: {kind!r}")
    if field_name is None and aggregate == "count":
        pass
    elif not isinstance(field_name, str) or field_name not in index:
        errors.append(f"unknown field in {channel}: {field_name!r}")
    elif kind == "quantitative" or aggregate in {"sum", "mean", "median"}:
        values = [row[index[field_name]] for row in rows if row[index[field_name]] is not None]
        if not values or not all(_is_number(value) for value in values):
            errors.append(f"field {field_name} is not numeric")
    title = definition.get("title")
    if title is not None and not isinstance(title, str):
        errors.append(f"title in {channel} must be text")
    if errors:
        return None, errors
    clean = {"type": str(kind)}
    if isinstance(field_name, str):
        clean["field"] = field_name
    if isinstance(aggregate, str):
        clean["aggregate"] = aggregate
    if isinstance(title, str):
        clean["title"] = title[:MAX_TITLE]
    return clean, []


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?|\.\d+")


def _normal(text: str) -> str:
    text = text.replace(",", "")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _numbers_in(text: str) -> set[str]:
    return {_normal(match) for match in _NUMBER.findall(text)}


def _cell_numbers(value: object) -> set[str]:
    if value is None or isinstance(value, bool):
        return set()
    if _is_number(value):
        try:
            exact = Decimal(str(value))
            found = {_normal(format(exact, "f"))}
            for places in (0, 1, 2):
                found.add(_normal(format(round(exact, places), "f")))
        except (ArithmeticError, ValueError):
            return set()
        return found
    return _numbers_in(str(value))


def summary_is_grounded(
    summary: str,
    question: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    row_limit: int,
) -> bool:
    """True when every number in the summary (above 10) comes from the rows or the question."""
    allowed: set[str] = {_normal(str(len(rows))), _normal(str(len(columns))), _normal(str(row_limit))}
    allowed |= _numbers_in(question)
    for row in rows:
        for cell in row:
            allowed |= _cell_numbers(cell)
    for number in _numbers_in(summary):
        try:
            small = float(number) <= 10 and float(number).is_integer()
        except ValueError:
            return False
        if not small and number not in allowed:
            return False
    return True
