"""Per-question model usage (tokens, GPU time) and process-wide totals.

The Ollama adapter reports each call's counts to ``record_call``. A question opens a
``usage_scope`` so its calls add up to one total; every call also feeds ``METRICS``, which the
metrics endpoint reads. Counters live in this process only and reset on restart.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

RECENT_QUESTIONS = 200


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    gpu_ms: int = 0
    calls: int = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, prompt_tokens: int, completion_tokens: int, gpu_ms: int) -> None:
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.gpu_ms += gpu_ms
        self.calls += 1


_current: ContextVar[Usage | None] = ContextVar("question_usage", default=None)


@dataclass
class UsageMetrics:
    """Thread-safe totals plus the most recent questions, newest last."""

    _lock: threading.Lock = field(default_factory=threading.Lock)
    totals: Usage = field(default_factory=Usage)
    questions: int = 0
    llm_errors: int = 0
    recent: deque[dict[str, int]] = field(default_factory=lambda: deque(maxlen=RECENT_QUESTIONS))

    def add_call(self, usage: Usage) -> None:
        with self._lock:
            self.totals.add(usage.prompt_tokens, usage.completion_tokens, usage.gpu_ms)

    def add_error(self) -> None:
        with self._lock:
            self.llm_errors += 1

    def add_question(self, usage: Usage, latency_ms: int) -> None:
        with self._lock:
            self.questions += 1
            self.recent.append(
                {
                    "tokens": usage.tokens,
                    "gpu_ms": usage.gpu_ms,
                    "llm_calls": usage.calls,
                    "latency_ms": latency_ms,
                }
            )

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            recent = list(self.recent)
            count = len(recent)
            return {
                "questions": self.questions,
                "llm_calls": self.totals.calls,
                "llm_errors": self.llm_errors,
                "prompt_tokens": self.totals.prompt_tokens,
                "completion_tokens": self.totals.completion_tokens,
                "gpu_ms": self.totals.gpu_ms,
                "avg_tokens_per_question": round(sum(r["tokens"] for r in recent) / count) if count else 0,
                "avg_gpu_ms_per_question": round(sum(r["gpu_ms"] for r in recent) / count) if count else 0,
                "recent": recent[-20:],
            }

    def reset(self) -> None:
        with self._lock:
            self.totals = Usage()
            self.questions = 0
            self.llm_errors = 0
            self.recent.clear()


METRICS = UsageMetrics()


@contextmanager
def bind_usage(usage: Usage) -> Iterator[Usage]:
    """Add the model calls made inside the block to ``usage``.

    A streamed answer resumes on a different thread at each step, so bind around each step
    rather than once around the whole stream.
    """
    token = _current.set(usage)
    try:
        yield usage
    finally:
        _current.reset(token)


def record_call(prompt_tokens: int, completion_tokens: int, gpu_ns: int) -> None:
    """Called by a model adapter after each call; ``gpu_ns`` is the model's own busy time."""
    call = Usage()
    call.add(max(prompt_tokens, 0), max(completion_tokens, 0), max(gpu_ns, 0) // 1_000_000)
    scope = _current.get()
    if scope is not None:
        scope.add(call.prompt_tokens, call.completion_tokens, call.gpu_ms)
    METRICS.add_call(call)


def record_error() -> None:
    METRICS.add_error()


def counts_from_ollama(payload: Mapping[str, object]) -> tuple[int, int, int]:
    """(prompt tokens, completion tokens, GPU ns) from an Ollama response body."""

    def number(key: str) -> int:
        value = payload.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return number("prompt_eval_count"), number("eval_count"), number("total_duration")
