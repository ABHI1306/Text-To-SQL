"""Hybrid (vector plus keyword) retrieval of the models relevant to a question."""

from __future__ import annotations

import contextlib
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from threading import Lock

from app.core.types import JsonValue, VectorMatch
from app.domain.mdl import Mdl, MdlColumn, MdlModel, Relationship
from app.ports.embedder import EmbedderPort
from app.ports.knowledge import Instruction, KnowledgeLookup, SqlPair
from app.ports.vector_store import VectorStorePort

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class RetrievedModel:
    """A model trimmed to its relevant columns, with its retrieval score."""
    name: str
    description: str
    table_reference: str
    columns: tuple[MdlColumn, ...]
    score: float


@dataclass(frozen=True)
class RetrievalResult:
    """Models, relationships, raw matches and approved knowledge found for a question."""
    models: tuple[RetrievedModel, ...]
    relationships: tuple[Relationship, ...]
    matches: tuple[VectorMatch, ...]
    pairs: tuple[SqlPair, ...] = ()
    instructions: tuple[Instruction, ...] = ()


class RetrievalCache:
    """A short shared cache of retrieval results.

    Retrieval sees schema text and approved knowledge only, never row data or a user, so sharing
    it between users is safe; the TTL bounds how stale an approved pair or instruction can be.
    """

    def __init__(
        self, seconds: float, entries: int = 256, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """Set the TTL, the entry cap and the clock (injectable for tests)."""
        self._seconds = seconds
        self._entries = entries
        self._clock = clock
        self._items: OrderedDict[tuple[object, ...], tuple[float, RetrievalResult]] = OrderedDict()
        self._lock = Lock()

    def get(self, key: tuple[object, ...]) -> RetrievalResult | None:
        """The cached result if present and not expired, else None."""
        with self._lock:
            hit = self._items.get(key)
            if hit is None or self._clock() - hit[0] >= self._seconds:
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return hit[1]

    def put(self, key: tuple[object, ...], result: RetrievalResult) -> None:
        """Store a result, evicting the oldest entries beyond the cap."""
        with self._lock:
            self._items[key] = (self._clock(), result)
            self._items.move_to_end(key)
            while len(self._items) > self._entries:
                self._items.popitem(last=False)

    def clear(self) -> None:
        """Drop every cached result."""
        with self._lock:
            self._items.clear()


class RetrievalService:
    """Finds the models for a question by blending vector and keyword scores."""
    def __init__(
        self,
        embedder: EmbedderPort,
        vector_store: VectorStorePort,
        *,
        keyword_weight: float = 0.35,
        relationship_expansion: bool = True,
        knowledge: KnowledgeLookup | None = None,
        cache: RetrievalCache | None = None,
    ) -> None:
        """Set the embedder, vector store and options; ``keyword_weight`` must be in 0..1."""
        if not 0 <= keyword_weight <= 1:
            raise ValueError("keyword_weight must be between 0 and 1")
        self._embedder = embedder
        self._vector_store = vector_store
        self._keyword_weight = keyword_weight
        self._relationship_expansion = relationship_expansion
        self._knowledge = knowledge
        self._cache = cache

    def retrieve(
        self,
        project_id: str,
        model_version: str,
        question: str,
        mdl: Mdl,
        *,
        limit: int = 5,
        max_columns_per_model: int = 12,
    ) -> RetrievalResult:
        """Models, relationships and knowledge for ``question``, using the cache when set."""
        if not question.strip():
            raise ValueError("Question cannot be empty")
        if limit < 1 or max_columns_per_model < 1:
            raise ValueError("Retrieval limits must be positive")
        key = (project_id, model_version, question, limit, max_columns_per_model)
        if self._cache is not None:
            hit = self._cache.get(key)
            if hit is not None:
                return hit
        result = self._retrieve(project_id, model_version, question, mdl, limit,
                                max_columns_per_model)
        if self._cache is not None:
            self._cache.put(key, result)
        return result

    def _retrieve(
        self,
        project_id: str,
        model_version: str,
        question: str,
        mdl: Mdl,
        limit: int,
        max_columns_per_model: int,
    ) -> RetrievalResult:
        """Embed, rank, pick models, expand through relationships and attach approved knowledge."""
        vectors = self._embedder.embed([question])
        if len(vectors) != 1:
            raise ValueError("Embedder must return exactly one vector for a question")
        matches = self._vector_store.search(
            project_id,
            model_version,
            vectors[0],
            limit=max(limit * 4, 12),
        )
        ranked = self._hybrid_rank(question, matches)
        model_by_name = {model.name: model for model in mdl.models}
        selected_scores: dict[str, float] = {}
        for score, match in ranked:
            name = _payload_string(match.payload, "model")
            if name in model_by_name:
                selected_scores[name] = max(score, selected_scores.get(name, 0))
        direct = sorted(selected_scores, key=selected_scores.__getitem__, reverse=True)[:limit]
        selected = set(direct)
        if self._relationship_expansion:
            for relationship in mdl.relationships:
                if selected.intersection(relationship.models):
                    selected.update(relationship.models)
        ordered = direct + sorted(selected - set(direct))
        models = tuple(
            _prune_model(
                model_by_name[name],
                selected_scores.get(name, 0.0),
                question,
                max_columns_per_model,
            )
            for name in ordered
            if name in model_by_name
        )
        relationships = tuple(
            relationship
            for relationship in mdl.relationships
            if set(relationship.models).issubset({model.name for model in models})
        )
        pairs: tuple[SqlPair, ...] = ()
        instructions: tuple[Instruction, ...] = ()
        if self._knowledge is not None:
            # Knowledge is advisory: when it is unavailable schema retrieval still works.
            with contextlib.suppress(Exception):
                context = self._knowledge.lookup(project_id, vectors[0])
                pairs, instructions = context.pairs, context.instructions
        return RetrievalResult(
            models=models,
            relationships=relationships,
            matches=tuple(match for _, match in ranked),
            pairs=pairs,
            instructions=instructions,
        )

    def _hybrid_rank(
        self,
        question: str,
        matches: Sequence[VectorMatch],
    ) -> list[tuple[float, VectorMatch]]:
        """Score matches as a blend of vector score and question-word overlap, best first."""
        query_tokens = _tokens(question)
        if not query_tokens:
            return [(match.score, match) for match in matches]
        scores: list[tuple[float, VectorMatch]] = []
        for match in matches:
            text = _payload_string(match.payload, "text")
            document_tokens = _tokens(text)
            lexical = len(query_tokens & document_tokens) / len(query_tokens)
            score = (1 - self._keyword_weight) * match.score + self._keyword_weight * lexical
            scores.append((score, match))
        return sorted(scores, key=lambda entry: entry[0], reverse=True)


def _tokens(text: str) -> set[str]:
    """Lowercase alphanumeric word set of ``text``."""
    return set(_TOKEN_PATTERN.findall(text.casefold()))


def _payload_string(payload: Mapping[str, JsonValue], key: str) -> str:
    """String value of ``key`` in a match payload, or an empty string."""
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _prune_model(
    model: MdlModel,
    score: float,
    question: str,
    max_columns: int,
) -> RetrievedModel:
    """Keep the columns that best match the question and primary keys, up to ``max_columns``."""
    query_tokens = _tokens(question)
    scored_columns: list[tuple[int, MdlColumn]] = []
    for column in model.columns:
        column_tokens = _tokens(f"{column.name} {column.description}")
        relevance = len(query_tokens & column_tokens)
        if relevance or column.is_primary_key:
            scored_columns.append((relevance + (1 if column.is_primary_key else 0), column))
    scored_columns.sort(key=lambda item: item[0], reverse=True)
    chosen = [column for _, column in scored_columns[:max_columns]]
    if not chosen:
        chosen = model.columns[:max_columns]
    return RetrievedModel(
        name=model.name,
        description=model.description,
        table_reference=model.table_reference,
        columns=tuple(chosen),
        score=score,
    )
