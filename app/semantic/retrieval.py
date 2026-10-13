from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.core.types import JsonValue, VectorMatch
from app.domain.mdl import Mdl, MdlColumn, MdlModel, Relationship
from app.ports.embedder import EmbedderPort
from app.ports.vector_store import VectorStorePort

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class RetrievedModel:
    name: str
    description: str
    table_reference: str
    columns: tuple[MdlColumn, ...]
    score: float


@dataclass(frozen=True)
class RetrievalResult:
    models: tuple[RetrievedModel, ...]
    relationships: tuple[Relationship, ...]
    matches: tuple[VectorMatch, ...]


class RetrievalService:
    def __init__(
        self,
        embedder: EmbedderPort,
        vector_store: VectorStorePort,
        *,
        keyword_weight: float = 0.35,
        relationship_expansion: bool = True,
    ) -> None:
        if not 0 <= keyword_weight <= 1:
            raise ValueError("keyword_weight must be between 0 and 1")
        self._embedder = embedder
        self._vector_store = vector_store
        self._keyword_weight = keyword_weight
        self._relationship_expansion = relationship_expansion

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
        if not question.strip():
            raise ValueError("Question cannot be empty")
        if limit < 1 or max_columns_per_model < 1:
            raise ValueError("Retrieval limits must be positive")
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
        return RetrievalResult(
            models=models,
            relationships=relationships,
            matches=tuple(match for _, match in ranked),
        )

    def _hybrid_rank(
        self,
        question: str,
        matches: Sequence[VectorMatch],
    ) -> list[tuple[float, VectorMatch]]:
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
    return set(_TOKEN_PATTERN.findall(text.casefold()))


def _payload_string(payload: Mapping[str, JsonValue], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _prune_model(
    model: MdlModel,
    score: float,
    question: str,
    max_columns: int,
) -> RetrievedModel:
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
