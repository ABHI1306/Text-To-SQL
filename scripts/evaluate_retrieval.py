from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast

from sqlglot import exp, parse
from sqlglot.errors import ParseError

from app.adapters.llm.ollama_embedder import OllamaEmbedderAdapter
from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.adapters.vector_store.qdrant import QdrantVectorStoreAdapter
from app.config import get_settings
from app.domain.mdl import Mdl
from app.semantic.retrieval import RetrievalService

DEFAULT_GOLDEN_SET = Path(__file__).resolve().parents[1] / "tests/data/retrieval_golden.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure retrieval recall against DBA-approved reference cases."
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--golden-set", type=Path, default=DEFAULT_GOLDEN_SET)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument(
        "--case-prefix",
        default="",
        help="Only score cases whose id starts with this prefix (e.g. ecom- or hr-).",
    )
    parser.add_argument(
        "--include-pending",
        action="store_true",
        help=(
            "Also score cases still awaiting DBA approval. The result is labelled "
            "provisional and must not be treated as the Day 3 baseline."
        ),
    )
    args = parser.parse_args()
    if args.k < 1:
        parser.error("--k must be positive")

    golden = json.loads(args.golden_set.read_text(encoding="utf-8"))
    if not isinstance(golden, dict) or not isinstance(golden.get("cases"), list):
        raise TypeError("Golden set must contain a cases array")
    cases = cast(list[dict[str, object]], golden["cases"])
    cases = [case for case in cases if str(case.get("id", "")).startswith(args.case_prefix)]
    approved = [
        case
        for case in cases
        if case.get("approval_status") == "approved" or args.include_pending
    ]
    pending = sum(case.get("approval_status") != "approved" for case in approved)
    unmeasured_pending = len(cases) - len(approved)
    if not approved:
        print(
            json.dumps(
                {
                    "status": "not_measurable",
                    "reason": "No cases have DBA approval.",
                    "approved_cases": 0,
                    "pending_cases": unmeasured_pending,
                    "recall_at_k": None,
                    "k": args.k,
                },
                indent=2,
            )
        )
        return 0

    settings = get_settings()
    if not settings.metadata_database_url:
        raise ValueError("METADATA_DATABASE_URL is required")
    repository = PostgresMetadataRepository(settings.metadata_database_url)
    version = repository.get_active_mdl_version(args.project_id)
    if version is None:
        raise ValueError("The project has no deployed semantic-model version")
    document = repository.get_mdl(args.project_id, version)
    if document is None:
        raise ValueError(f"Deployed MDL revision {version} is missing")
    mdl_document = {
        key: value
        for key, value in document.items()
        if key not in {"project_id", "revision"}
    }
    mdl = Mdl.model_validate(mdl_document)
    vector_store = QdrantVectorStoreAdapter(settings.qdrant_url)
    try:
        retrieval = RetrievalService(
            OllamaEmbedderAdapter(settings.ollama_url, settings.embedding_model),
            vector_store,
        )
        scores: list[float] = []
        results: list[dict[str, object]] = []
        for case in approved:
            case_id = case.get("id")
            question = case.get("question")
            expected_models = case.get("expected_models")
            reference_sql = case.get("reference_sql")
            if (
                not isinstance(case_id, str)
                or not isinstance(question, str)
                or not isinstance(expected_models, list)
                or not expected_models
                or not all(isinstance(name, str) for name in expected_models)
            ):
                raise ValueError(f"Golden case {case_id!r} is malformed")
            case_is_approved = case.get("approval_status") == "approved"
            if case_is_approved:
                if not isinstance(reference_sql, str) or not reference_sql.strip():
                    raise ValueError(f"Approved golden case {case_id!r} has no reference SQL")
                try:
                    statements = parse(reference_sql, read="postgres")
                except ParseError as error:
                    raise ValueError(
                        f"Approved golden case {case_id!r} has invalid PostgreSQL reference SQL"
                    ) from error
                if len(statements) != 1 or not isinstance(statements[0], exp.Query):
                    raise ValueError(
                        f"Approved golden case {case_id!r} must have one read-only query"
                    )
            found = {
                model.name
                for model in retrieval.retrieve(
                    args.project_id,
                    str(version),
                    question,
                    mdl,
                    limit=args.k,
                ).models
            }
            expected = set(cast(list[str], expected_models))
            score = len(found & expected) / len(expected)
            scores.append(score)
            results.append(
                {
                    "id": case_id,
                    "recall": score,
                    "expected_models": sorted(expected),
                    "retrieved_models": sorted(found),
                    "approved": case_is_approved,
                    "reference_sql_parse_valid": True if case_is_approved else None,
                }
            )
    finally:
        vector_store.close()

    mean_recall = sum(scores) / len(scores)
    print(
        json.dumps(
            {
                "status": "measured" if pending == 0 else "provisional_includes_unapproved_cases",
                "approved_cases": len(scores) - pending,
                "pending_cases_scored": pending,
                "pending_cases_not_scored": unmeasured_pending,
                "k": args.k,
                "recall_at_k": mean_recall,
                "target": 0.9,
                "target_met": mean_recall >= 0.9 if pending == 0 else None,
                "provisional_target_met": mean_recall >= 0.9,
                "cases": results,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
