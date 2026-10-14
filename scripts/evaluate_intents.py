"""Measure intent routing and follow-up rewriting against the real small model (Ollama)."""

from __future__ import annotations

import argparse

from app.adapters.llm.ollama import OllamaLLMAdapter
from app.config import get_settings
from app.evaluation.conversation import (
    evaluate_chains,
    evaluate_intents,
    load_chains,
    load_intent_cases,
)
from tests.support.ecommerce_mdl import build_mdl


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="", help="Defaults to SMALL_MODEL, then SQL_MODEL.")
    parser.add_argument("--floor", type=float, default=0.7, help="Minimum intent accuracy.")
    args = parser.parse_args()
    settings = get_settings()
    llm = OllamaLLMAdapter(settings.ollama_url, args.model or settings.small_model or settings.sql_model)
    names = [model.name for model in build_mdl().models]
    intents = evaluate_intents(llm, load_intent_cases(), names)
    chains = evaluate_chains(llm, load_chains(), names)
    print(f"intent accuracy: {intents.correct}/{intents.total} = {intents.accuracy:.0%}")
    print(f"chain intent accuracy: {chains.intent_accuracy:.0%}")
    print(f"rewrite faithfulness: {chains.rewrites_correct}/{chains.rewrites_scored}")
    for failure in (*intents.failures, *chains.failures):
        print(f"  miss {failure}")
    return 0 if intents.accuracy >= args.floor else 1


if __name__ == "__main__":
    raise SystemExit(main())
