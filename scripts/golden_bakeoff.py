"""Run the full retrieval golden set through local models (Day 3 bake-off).

Prompt-adherence smoke test only: it checks that the model answers with one
schema-qualified read-only SELECT over known tables and that the expected
models appear in it. Execution accuracy needs the compiler path (Day 4) and
DBA-approved reference SQL, so it is not measured here.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from collections import defaultdict
from pathlib import Path
from urllib.error import URLError

import sqlglot
from sqlglot import exp

from scripts.model_bakeoff import collect_tables, ollama_json, ollama_tags

ROOT = Path(__file__).resolve().parents[1]
NON_COLUMN = {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"}


def schema_from_ddl(ddl: str) -> dict[tuple[str, str], list[str]]:
    tables: dict[tuple[str, str], list[str]] = {}
    for match in re.finditer(r"CREATE TABLE (\w+)\.(\w+) \((.*?)\n\);", ddl, re.DOTALL):
        schema, table, body = match.groups()
        columns = []
        for line in body.splitlines():
            word = line.strip().split(" ")[0].strip(",")
            if word and word.upper() not in NON_COLUMN:
                columns.append(word)
        tables[(schema, table)] = columns
    return tables


def render_schema(tables: dict[tuple[str, str], list[str]]) -> str:
    return "\n".join(f"{s}.{t}({', '.join(c)})" for (s, t), c in tables.items())


def allowed(sql: str, known: set[tuple[str, str]]) -> bool:
    try:
        parsed = sqlglot.parse(sql, read="postgres")
    except sqlglot.errors.ParseError:
        return False
    if len(parsed) != 1 or not isinstance(parsed[0], exp.Query):
        return False
    query = parsed[0]
    names = {c.name.lower() for c in query.find_all(exp.CTE)}
    relations = [t for t in query.find_all(exp.Table) if t.name.lower() not in names]
    if not relations and not names:
        return False
    if any(isinstance(n, (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Command))
           for n in query.walk()):
        return False
    return all((t.db.lower(), t.name.lower()) in known and not t.catalog for t in relations)


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct * (len(ordered) - 1)))]


def run_model(model: str, cases: list[dict], schema_text: str,
              known: set[tuple[str, str]]) -> dict:
    info = next((m for m in ollama_tags().get("models", []) if m.get("name") == model), None)
    if info is None:
        raise ValueError(f"Model {model!r} is not installed in the local Ollama server")
    started = time.perf_counter()
    results = []
    for case in cases:
        prompt = (
            'Return JSON only in the form {"sql":"..."}. Write one PostgreSQL read-only '
            "SELECT. Use only the listed tables and columns, always schema-qualified.\n"
            f"Schema:\n{schema_text}\nQuestion: {case['question']}"
        )
        t0 = time.perf_counter()
        out = ollama_json("/api/generate", {
            "model": model, "prompt": prompt, "format": "json", "stream": False,
            "options": {"temperature": 0},
        })
        seconds = time.perf_counter() - t0
        try:
            sql = str(json.loads(str(out.get("response", "")))["sql"])
            tables = collect_tables(sql)
            valid = allowed(sql, known)
        except (KeyError, TypeError, ValueError, sqlglot.errors.ParseError):
            sql, tables, valid = "", set(), False
        expected = set(case["expected_models"])
        eval_count, eval_ns = out.get("eval_count"), out.get("eval_duration")
        results.append({
            "id": case["id"], "difficulty": case["difficulty"],
            "valid": valid, "expected_models_present": expected <= tables,
            "missing_models": sorted(expected - tables), "seconds": round(seconds, 2),
            "tokens_per_second": round(eval_count / (eval_ns / 1e9), 2)
            if eval_count and eval_ns else None,
            "sql": sql,
        })
    by_difficulty: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_difficulty[r["difficulty"]].append(r)
    times = [r["seconds"] for r in results]
    tps = [r["tokens_per_second"] for r in results if r["tokens_per_second"]]
    return {
        "model": model, "digest": info.get("digest"), "cases": len(results),
        "valid": sum(r["valid"] for r in results),
        "expected_models_present": sum(r["expected_models_present"] for r in results),
        "valid_rate": round(sum(r["valid"] for r in results) / len(results), 3),
        "model_recall_rate": round(
            sum(r["expected_models_present"] for r in results) / len(results), 3),
        "by_difficulty": {
            d: {"n": len(rs), "valid": sum(r["valid"] for r in rs),
                "expected_models_present": sum(r["expected_models_present"] for r in rs)}
            for d, rs in sorted(by_difficulty.items())},
        "latency_seconds": {"p50": round(statistics.median(times), 2),
                            "p95": round(percentile(times, 0.95), 2)},
        "tokens_per_second_median": round(statistics.median(tps), 2) if tps else None,
        "wall_time_seconds": round(time.perf_counter() - started, 1),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=["llama3:latest"])
    parser.add_argument("--golden", type=Path, default=ROOT / "tests/data/retrieval_golden.json")
    parser.add_argument("--ddl", type=Path, default=ROOT / "scripts/db/init-demo.sql")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.golden.read_text())["cases"]
    tables = schema_from_ddl(args.ddl.read_text())
    known = {(s.lower(), t.lower()) for s, t in tables}
    try:
        reports = [run_model(m, cases, render_schema(tables), known) for m in args.models]
    except (URLError, TimeoutError, ValueError) as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(reports, indent=2) + "\n")
    for r in reports:
        print(f"{r['model']}: valid {r['valid']}/{r['cases']}, expected models present "
              f"{r['expected_models_present']}/{r['cases']}, p50 {r['latency_seconds']['p50']}s, "
              f"p95 {r['latency_seconds']['p95']}s; digest={r['digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
