"""Intent and follow-up evaluation sets, their runner, and a five-turn conversation via AskService.

CI uses a scripted model (deterministic, checks the data and the scoring); with a reachable
Ollama the same sets run against the real small model.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Any

import httpx
import pytest

from app.adapters.llm.ollama import OllamaLLMAdapter
from app.adapters.metadata.threads import InMemoryThreadStore
from app.agent.service import AskService
from app.evaluation.conversation import (
    Chain,
    evaluate_chains,
    evaluate_intents,
    load_chains,
    load_intent_cases,
)
from tests.support.audit import RecordingAudit
from tests.support.ecommerce_mdl import build_mdl, build_policies, build_principal
from tests.test_agent_graph import FakeRetrieval, FakeWarehouse, ScriptedLlm, sql

MDL = build_mdl()
MODEL_NAMES = [m.name for m in MDL.models]
CASES = load_intent_cases()
CHAINS = load_chains()
MESSAGE = re.compile(r"<message>\n(.*)\n</message>")


class ScriptedRouter:
    """Small-model stand-in that answers every prompt from the evaluation data."""

    def __init__(self, wrong: set[str] | None = None) -> None:
        self.wrong = wrong or set()
        self.intents: dict[str, str] = {c.message: c.intent for c in CASES}
        self.rewrites: dict[str, str] = {}
        for chain in CHAINS:
            for turn in chain.turns:
                self.rewrites[turn.message] = turn.reference_standalone
                self.intents[turn.reference_standalone] = turn.intent
        self.calls = 0

    def generate(self, prompt: str, *, temperature: float = 0.0, schema: Any = None) -> str:
        self.calls += 1
        message = json.loads(MESSAGE.search(prompt).group(1))  # type: ignore[union-attr]
        if "<history>" in prompt:
            return json.dumps({"question": self.rewrites[message]})
        intent = self.intents[message]
        if message in self.wrong:
            intent = "respond" if intent == "data_question" else "data_question"
        reply = {"data_question": "", "clarify": "Which metric do you mean?", "respond": "Hello!"}
        return json.dumps({"intent": intent, "message": reply[intent]})


# ------------------------------------------------------------------ the data sets


def test_the_intent_set_has_thirty_prompts_covering_all_three_intents() -> None:
    assert len(CASES) == 30 and len({c.message for c in CASES}) == 30
    counts = Counter(c.intent for c in CASES)
    assert set(counts) == {"data_question", "clarify", "respond"}
    assert all(n >= 8 for n in counts.values())


def test_the_chain_set_has_ten_chains_with_pronouns_ellipsis_and_topic_switches() -> None:
    assert len(CHAINS) == 10 and all(len(c.turns) >= 3 for c in CHAINS)
    kinds = Counter(t.kind for c in CHAINS for t in c.turns)
    assert kinds["pronoun"] >= 4 and kinds["ellipsis"] >= 4 and kinds["topic_switch"] >= 2
    messages = [c.message for c in CASES] + [t.message for ch in CHAINS for t in ch.turns]
    assert len(messages) == len(set(messages))  # the scripted model keys on the message


def test_the_reference_rewrites_satisfy_their_own_checks() -> None:
    from app.evaluation.conversation import rewrite_is_faithful

    for chain in CHAINS:
        for turn in chain.turns:
            assert rewrite_is_faithful(turn, turn.reference_standalone), (chain.id, turn.message)


# --------------------------------------------------------------------- the runner


def test_a_perfect_model_scores_one_hundred_percent_on_both_sets() -> None:
    intents = evaluate_intents(ScriptedRouter(), CASES, MODEL_NAMES)
    chains = evaluate_chains(ScriptedRouter(), CHAINS, MODEL_NAMES)
    assert intents.accuracy == 1.0 and intents.failures == ()
    assert chains.intent_accuracy == 1.0 and chains.rewrite_accuracy == 1.0 and chains.failures == ()
    assert chains.turns == sum(len(c.turns) for c in CHAINS)
    assert chains.rewrites_scored == chains.turns - len(CHAINS)  # first turns have no rewrite


def test_the_runner_counts_misses_exactly_and_fills_the_confusion_matrix() -> None:
    wrong = {CASES[0].message, CASES[1].message, CASES[20].message}
    report = evaluate_intents(ScriptedRouter(wrong), CASES, MODEL_NAMES)
    assert report.correct == 27 and report.accuracy == pytest.approx(0.9)
    assert len(report.failures) == 3 and report.failures[0].startswith("intent-01")
    assert report.confusion["data_question"]["respond"] == 2
    assert report.confusion["clarify"]["data_question"] == 1


def test_a_complete_message_skips_the_rewrite_so_the_old_topic_cannot_leak() -> None:
    router = ScriptedRouter()
    switch = next(t for c in CHAINS for t in c.turns if t.kind == "topic_switch")
    router.rewrites[switch.message] = switch.reference_standalone + " revenue category 2024"
    report = evaluate_chains(router, CHAINS, MODEL_NAMES)
    assert report.rewrites_correct == report.rewrites_scored


def test_a_rewrite_that_drops_the_antecedent_is_scored_wrong() -> None:
    router = ScriptedRouter()
    pronoun = next(t for c in CHAINS for t in c.turns if t.kind == "pronoun")
    router.rewrites[pronoun.message] = "something unrelated"
    router.intents["something unrelated"] = pronoun.intent
    report = evaluate_chains(router, CHAINS, MODEL_NAMES)
    assert report.rewrites_correct == report.rewrites_scored - 1
    assert any("pronoun" in f for f in report.failures)


def test_an_unavailable_model_is_all_misses_not_a_crash() -> None:
    class Down:
        def generate(self, *_: Any, **__: Any) -> str:
            raise RuntimeError("down")

    assert evaluate_intents(Down(), CASES, MODEL_NAMES).correct == 0
    assert evaluate_chains(Down(), CHAINS, MODEL_NAMES).intent_correct == 0


def _ollama() -> OllamaLLMAdapter:
    url = os.getenv("OLLAMA_URL", "")
    model = os.getenv("SMALL_MODEL") or os.getenv("SQL_MODEL", "")
    if not url or not model:
        pytest.skip("OLLAMA_URL and SMALL_MODEL (or SQL_MODEL) are required for the live evaluation")
    try:
        httpx.get(f"{url.rstrip('/')}/api/tags", timeout=3).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("Ollama is not reachable")
    return OllamaLLMAdapter(url, model)


def test_live_intent_accuracy_against_the_real_small_model() -> None:
    llm = _ollama()
    intents = evaluate_intents(llm, CASES, MODEL_NAMES)
    chains = evaluate_chains(llm, [Chain(c.id, c.turns) for c in CHAINS], MODEL_NAMES)
    print(
        f"intent accuracy {intents.accuracy:.0%}; chain intent {chains.intent_accuracy:.0%}; "
        f"rewrite {chains.rewrite_accuracy:.0%}\n" + "\n".join(intents.failures + chains.failures)
    )
    assert intents.accuracy >= float(os.getenv("INTENT_ACCURACY_FLOOR", "0.7"))


# ------------------------------------------------------ five turns through AskService


def test_a_five_turn_conversation_keeps_context_and_only_queries_for_data_questions() -> None:
    chain = next(c for c in CHAINS if c.id == "chain-08-chitchat-between")
    first, chat, third = chain.turns
    script: dict[str, tuple[str, str, str]] = {  # message -> (standalone, intent, reply)
        "How many orders?": ("How many orders?", "data_question", ""),
        "and by status?": ("How many orders by status?", "data_question", ""),
        first.message: (first.message, "data_question", ""),
        chat.message: (chat.message, "respond", "You are welcome!"),
        third.message: (third.reference_standalone, "data_question", ""),
    }

    class Small:
        def __init__(self) -> None:
            self.rewrite_prompts: list[str] = []

        def generate(self, prompt: str, *, temperature: float = 0.0, schema: Any = None) -> str:
            message = json.loads(MESSAGE.search(prompt).group(1))  # type: ignore[union-attr]
            if "<history>" in prompt:
                self.rewrite_prompts.append(prompt)
                return json.dumps({"question": script[message][0]})
            standalone = next(v for v in script.values() if v[0] == message)
            return json.dumps({"intent": standalone[1], "message": standalone[2]})

    small, threads, audit = Small(), InMemoryThreadStore(), RecordingAudit()
    llm = ScriptedLlm(sql("SELECT order_id FROM orders"))
    warehouse = FakeWarehouse()
    service = AskService(None, None, llm, warehouse, audit=audit, small_llm=small, threads=threads)  # type: ignore[arg-type]
    service._retrieval = FakeRetrieval()  # type: ignore[assignment]
    principal = build_principal("u1", {"region": "EU"})

    def ask(question: str, thread: str | None = None) -> Any:
        return service.ask("p", 1, MDL, principal, build_policies(), question, thread_id=thread)

    one = ask("How many orders?")
    thread = one.thread_id
    two = ask("and by status?", thread)
    three = ask(first.message, thread)
    four = ask(chat.message, thread)
    five = ask(third.message, thread)

    assert all(r.ok and r.thread_id == thread for r in (one, two, three, four, five))
    assert [r.intent for r in (one, two, three, four, five)] == ["data_question"] * 3 + ["respond", "data_question"]
    assert four.reply == "You are welcome!" and four.rows == [] and four.logical_sql is None
    assert len(warehouse.executed) == 4  # the chit-chat turn never reached the database
    assert two.events[0].type == "rewrite" and two.events[0].data["question"] == "How many orders by status?"
    assert five.events[0].data["question"] == third.reference_standalone

    stored = threads.history(thread, "u1", limit=10)
    assert [t.question for t in stored] == [
        "How many orders?", "and by status?", first.message, chat.message, third.message,
    ]
    assert stored[3].logical_sql is None and stored[3].answer == "You are welcome!"
    assert stored[0].logical_sql == "SELECT order_id FROM orders"
    # The last rewrite saw the latest data question and the chit-chat after it, not older turns.
    last_prompt = small.rewrite_prompts[-1]
    assert first.message in last_prompt and chat.message in last_prompt
    assert "How many orders by status?" not in last_prompt
    # Another user cannot continue or read the thread.
    with pytest.raises(PermissionError):
        service.ask("p", 1, MDL, build_principal("u2", {"region": "EU"}), build_policies(), "How many orders?", thread_id=thread)
    assert audit.actions().count("question_asked") == 6  # five turns plus the refused intruder
