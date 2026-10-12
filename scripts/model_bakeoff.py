import argparse
import json
import os
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import sqlglot
from sqlglot import exp

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
ALLOWED_TABLES = {
    "categories",
    "customers",
    "orders",
    "order_items",
    "products",
    "payments",
}
CASES = [
    ("How many orders are there?", {"orders"}),
    ("Count customers by region.", {"customers"}),
    ("Show total units sold per product.", {"order_items", "products"}),
    ("Show payment totals by method.", {"payments"}),
    ("Find each customer's order count.", {"customers", "orders"}),
    ("Show average order value by region.", {"orders", "payments"}),
    ("List products with their category names.", {"products"}),
    ("Show the five most recent orders.", {"orders"}),
    ("Count orders by status.", {"orders"}),
    ("Show revenue by product.", {"order_items", "products"}),
]
SCHEMA = """
ecommerce.customers(customer_id, first_name, last_name, region)
ecommerce.orders(order_id, customer_id, ordered_at, status, region)
ecommerce.order_items(order_item_id, order_id, product_id, quantity, unit_price)
ecommerce.categories(category_id, name)
ecommerce.products(product_id, category_id, name, unit_price)
ecommerce.payments(payment_id, order_id, amount, method, status, paid_at)
"""


def ollama_json(path: str, payload: dict[str, object]) -> dict[str, object]:
    request = Request(
        f"{OLLAMA_URL}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=300) as response:
        return json.loads(response.read())


def ollama_tags() -> dict[str, object]:
    request = Request(f"{OLLAMA_URL}/api/tags")
    with urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def collect_tables(sql: str) -> set[str]:
    parsed = sqlglot.parse(sql, read="postgres")
    if len(parsed) != 1 or not isinstance(parsed[0], exp.Query):
        return set()
    return {table.name.lower() for table in parsed[0].find_all(exp.Table)}


def is_allowed_query(sql: str, tables: set[str]) -> bool:
    parsed = sqlglot.parse(sql, read="postgres")
    if len(parsed) != 1 or not isinstance(parsed[0], exp.Query) or not tables:
        return False
    query = parsed[0]
    relations = list(query.find_all(exp.Table))
    return (
        tables <= ALLOWED_TABLES
        and all(
            table.db.lower() == "ecommerce" and not table.catalog
            for table in relations
        )
        and not any(
            isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Command))
            for node in query.walk()
        )
    )


def run_model(model: str, limit: int) -> dict[str, object]:
    started = time.perf_counter()
    model_list = ollama_tags()
    model_info = next(
        (entry for entry in model_list.get("models", []) if entry.get("name") == model),
        None,
    )
    if model_info is None:
        raise ValueError(f"Model {model!r} is not installed in the local Ollama server")

    results = []
    for question, expected_tables in CASES[:limit]:
        prompt = (
            "Return JSON only in the form {\"sql\":\"...\"}. Write one PostgreSQL "
            "read-only SELECT. Use only the listed tables and columns. Do not use "
            "physical tables outside this schema.\\n"
            f"Schema:\\n{SCHEMA}\\nQuestion: {question}"
        )
        result = ollama_json(
            "/api/generate",
            {
                "model": model,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "options": {"temperature": 0},
            },
        )
        output = str(result.get("response", ""))
        try:
            sql = str(json.loads(output)["sql"])
            actual_tables = collect_tables(sql)
            valid = is_allowed_query(sql, actual_tables)
        except (KeyError, TypeError, ValueError, sqlglot.errors.ParseError):
            sql = ""
            actual_tables = set()
            valid = False
        results.append(
            {
                "question": question,
                "expected_tables": sorted(expected_tables),
                "actual_tables": sorted(actual_tables),
                "structured_and_schema_valid": valid,
                "required_tables_present": expected_tables <= actual_tables,
                "response": sql,
                "generation_duration_ns": result.get("total_duration"),
                "eval_count": result.get("eval_count"),
                "eval_duration_ns": result.get("eval_duration"),
            }
        )

    return {
        "model": model,
        "digest": model_info.get("digest"),
        "cases_run": len(results),
        "valid_responses": sum(bool(result["structured_and_schema_valid"]) for result in results),
        "required_tables_present": sum(bool(result["required_tables_present"]) for result in results),
        "schema_validity_rate": (
            sum(bool(result["structured_and_schema_valid"]) for result in results)
            / len(results)
        ),
        "wall_time_seconds": round(time.perf_counter() - started, 3),
        "results": results,
        "limitations": [
            "No semantic compiler or validator exists yet, so execution accuracy is not measured.",
            "Schema-validity is only a prompt-adherence smoke metric, not SQL correctness.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--models",
        nargs="+",
        default=os.getenv("MODEL_CANDIDATES", "llama3:latest").split(),
    )
    parser.add_argument("--limit", type=int, choices=range(1, 11), default=10)
    parser.add_argument("--output", type=Path, default=Path("reports/day1-model-smoke.json"))
    args = parser.parse_args()
    try:
        reports = [run_model(model, args.limit) for model in args.models]
    except (URLError, TimeoutError, ValueError) as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(reports, indent=2) + "\n", encoding="utf-8")
    for report in reports:
        print(
            f"{report['model']}: {report['valid_responses']}/{report['cases_run']} "
            f"schema-valid responses; digest={report['digest']}"
        )
    print(f"Saved detailed output to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
