"""Routing and follow-up evaluation: how often the small model picks the right intent and writes
a faithful standalone question. Runs the same rewrite and routing functions as the graph, so a
scripted model checks the plumbing in CI and a real one (Ollama) measures quality.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.agent.graph import rewrite_question, route_intent
from app.ports.llm import LLMPort

DATA_DIR = Path(__file__).resolve().parents[2] / "tests" / "data"
INTENT_SET = DATA_DIR / "intent_eval.json"
CHAIN_SET = DATA_DIR / "followup_chains.json"
INTENTS = ("data_question", "clarify", "respond")


@dataclass(frozen=True)
class IntentCase:
    id: str
    message: str
    intent: str


@dataclass(frozen=True)
class ChainTurn:
    message: str
    intent: str
    kind: str
    reference_standalone: str
    must_contain: tuple[str, ...] = ()
    must_not_contain: tuple[str, ...] = ()


@dataclass(frozen=True)
class Chain:
    id: str
    turns: tuple[ChainTurn, ...]


@dataclass(frozen=True)
class IntentReport:
    total: int
    correct: int
    confusion: dict[str, dict[str, int]]
    failures: tuple[str, ...]

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


@dataclass(frozen=True)
class ChainReport:
    turns: int
    intent_correct: int
    rewrites_scored: int
    rewrites_correct: int
    failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def intent_accuracy(self) -> float:
        return self.intent_correct / self.turns if self.turns else 0.0

    @property
    def rewrite_accuracy(self) -> float:
        return self.rewrites_correct / self.rewrites_scored if self.rewrites_scored else 0.0


def load_intent_cases(path: Path = INTENT_SET) -> list[IntentCase]:
    document = json.loads(path.read_text(encoding="utf-8"))
    cases = [IntentCase(c["id"], c["message"], c["intent"]) for c in document["cases"]]
    if any(case.intent not in INTENTS for case in cases):
        raise ValueError("Unknown intent label in the evaluation set")
    return cases


def load_chains(path: Path = CHAIN_SET) -> list[Chain]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return [
        Chain(
            chain["id"],
            tuple(
                ChainTurn(
                    t["message"], t["intent"], t["kind"], t["reference_standalone"],
                    tuple(t.get("must_contain", ())), tuple(t.get("must_not_contain", ())),
                )
                for t in chain["turns"]
            ),
        )
        for chain in document["chains"]
    ]


def evaluate_intents(
    llm: LLMPort, cases: Sequence[IntentCase], model_names: Sequence[str]
) -> IntentReport:
    confusion: dict[str, Counter[str]] = {name: Counter() for name in INTENTS}
    failures: list[str] = []
    correct = 0
    for case in cases:
        try:
            predicted, _ = route_intent(llm, case.message, model_names)
        except Exception:  # noqa: BLE001 - an unavailable model is a miss, not a crash
            predicted = "error"
        confusion[case.intent][predicted] += 1
        if predicted == case.intent:
            correct += 1
        else:
            failures.append(f"{case.id}: expected {case.intent}, got {predicted}")
    return IntentReport(
        len(cases), correct, {k: dict(v) for k, v in confusion.items()}, tuple(failures)
    )


def rewrite_is_faithful(turn: ChainTurn, standalone: str) -> bool:
    text = standalone.casefold()
    return all(w.casefold() in text for w in turn.must_contain) and not any(
        w.casefold() in text for w in turn.must_not_contain
    )


def evaluate_chains(
    llm: LLMPort, chains: Sequence[Chain], model_names: Sequence[str]
) -> ChainReport:
    turns = intent_correct = scored = rewrites_correct = 0
    failures: list[str] = []
    for chain in chains:
        history: list[tuple[str, str | None]] = []
        for number, turn in enumerate(chain.turns, 1):
            turns += 1
            label = f"{chain.id}#{number} ({turn.kind})"
            try:
                standalone = (
                    rewrite_question(llm, turn.message, history) if history else turn.message
                )
                predicted, _ = route_intent(llm, standalone, model_names)
            except Exception:  # noqa: BLE001 - an unavailable model is a miss, not a crash
                standalone, predicted = turn.message, "error"
            if history:
                scored += 1
                if rewrite_is_faithful(turn, standalone):
                    rewrites_correct += 1
                else:
                    failures.append(f"{label}: rewrite {standalone!r}")
            if predicted == turn.intent:
                intent_correct += 1
            else:
                failures.append(f"{label}: expected {turn.intent}, got {predicted}")
            # Like production, only a data question leaves SQL behind; chit-chat leaves None.
            history.append((standalone, "ran" if predicted == "data_question" else None))
    return ChainReport(turns, intent_correct, scored, rewrites_correct, tuple(failures))
