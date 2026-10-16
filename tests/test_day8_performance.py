"""Day 8 performance and safety guards: compile cache, retrieval cache, rate limits, evaluation diff."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from fastapi import HTTPException

from app.api.dependencies import require_capability
from app.api.rate_limit import SlidingWindowLimiter
from app.auth.models import AuthContext
from app.semantic.retrieval import RetrievalCache, RetrievalService
from app.semantic.runner import CompileCache, compile_cache, compile_for
from scripts.compare_eval import clusters, diff
from tests.support.ecommerce_mdl import build_mdl, build_policies, build_principal, load_cases
from tests.test_day3_semantic import MODEL, FakeEmbedder, FakeVectorStore

OVERLAY = load_cases()["model_overlay"]
MDL = build_mdl(OVERLAY)
POLICIES = build_policies(OVERLAY)


def test_compiled_output_is_never_shared_between_policy_contexts() -> None:
    compile_cache.clear()
    west = build_principal("west", {"region": "West"})
    south = build_principal("south", {"region": "South"})
    sql = "SELECT order_id FROM orders"

    first_west = compile_for(sql, MDL, west, POLICIES)
    first_south = compile_for(sql, MDL, south, POLICIES)
    again_west = compile_for(sql, MDL, west, POLICIES)

    assert not isinstance(first_west, tuple) and not isinstance(first_south, tuple)
    assert first_west.policy_context != first_south.policy_context
    assert "South" in str(first_south.parameters.values())
    assert "South" not in str(first_west.parameters.values())
    assert again_west is first_west  # same principal, same inputs: served from the cache


def test_changing_a_policy_or_attribute_changes_the_cache_key() -> None:
    principal = build_principal("u", {"region": "West"})
    key = CompileCache.key("SELECT 1", MDL, principal, POLICIES)
    assert key != CompileCache.key("SELECT 1", MDL, principal, [])
    assert key != CompileCache.key("SELECT 1", MDL, build_principal("u", {"region": "East"}), POLICIES)
    assert key != CompileCache.key("SELECT 2", MDL, principal, POLICIES)


def test_failed_compiles_are_not_cached() -> None:
    compile_cache.clear()
    principal = build_principal("west", {"region": "West"})
    assert isinstance(compile_for("DELETE FROM orders", MDL, principal, POLICIES), tuple)
    assert compile_cache.get(CompileCache.key("DELETE FROM orders", MDL, principal, POLICIES)) is None


class CountingEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        self.calls += 1
        return super().embed(texts)


def test_retrieval_cache_reuses_results_until_it_expires() -> None:
    now = [0.0]
    embedder = CountingEmbedder()
    cache = RetrievalCache(60, clock=lambda: now[0])
    service = RetrievalService(embedder, FakeVectorStore(), cache=cache)

    first = service.retrieve("p", "1", "customer region", MODEL)
    assert service.retrieve("p", "1", "customer region", MODEL) is first
    assert embedder.calls == 1
    service.retrieve("p", "2", "customer region", MODEL)  # a new model version is a new key
    assert embedder.calls == 2
    now[0] = 61
    service.retrieve("p", "1", "customer region", MODEL)
    assert embedder.calls == 3


def test_sliding_window_limiter_blocks_then_recovers() -> None:
    limiter = SlidingWindowLimiter(60)
    assert [limiter.check("u", 2, now=0), limiter.check("u", 2, now=1)] == [None, None]
    wait = limiter.check("u", 2, now=2)
    assert wait is not None and 57 <= wait <= 59
    assert limiter.check("other", 2, now=2) is None
    assert limiter.check("u", 2, now=61) is None


def _context(user_id: str) -> AuthContext:
    return AuthContext(
        user_id=user_id, username=user_id, role_name="User",
        capabilities=frozenset({"ask_question"}), project_ids=frozenset(),
        must_change_password=False,
    )


def test_route_dependency_returns_429_with_retry_after() -> None:
    check: Any = require_capability("ask_question", per_minute=1)
    assert check(_context("rate-user")).user_id == "rate-user"
    with pytest.raises(HTTPException) as caught:
        check(_context("rate-user"))
    assert caught.value.status_code == 429
    assert "Retry-After" in (caught.value.headers or {})
    assert check(_context("another-user")).user_id == "another-user"
    assert check.required_capability == "ask_question"


def test_failure_clusters_and_diff() -> None:
    old = {"cases": [
        {"id": "a", "outcome": "wrong_rows"}, {"id": "b", "outcome": "correct"},
        {"id": "c", "outcome": "failed", "failure": "UNKNOWN_COLUMN"},
    ]}
    new = {"cases": [
        {"id": "a", "outcome": "correct"}, {"id": "b", "outcome": "wrong_rows"},
        {"id": "c", "outcome": "reference_invalid"},
    ]}
    assert dict(clusters(old)) == {"generation error": 1, "compile": 1}
    assert diff(new, old) == (["a"], ["b"])
