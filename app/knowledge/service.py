"""Instructions, approved SQL pairs and the feedback-to-proposal workflow.

Knowledge is advisory prompt data, never authority: a pair only becomes retrievable once a user
holding ``approve_pairs`` approved it, its SQL is re-checked against the deployed model whenever
it is saved or approved, and every state change is audited in the transaction that makes it.
The vector store is an index only: whatever it returns is re-read from the repository, so a
deleted, rejected or foreign record can never come back.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.types import AuditEvent, VectorRecord
from app.domain.mdl import Mdl
from app.ports.embedder import EmbedderPort
from app.ports.knowledge import (
    Feedback,
    Instruction,
    KnowledgeContext,
    KnowledgeRepoPort,
    PairStatus,
    Rating,
    SqlPair,
)
from app.ports.vector_store import VectorStorePort
from app.semantic.compiler import compile_query

PAIRS_NAMESPACE = "knowledge:sql_pairs"
INSTRUCTIONS_NAMESPACE = "knowledge:instructions"
PAIR_LIMIT = 3
INSTRUCTION_LIMIT = 8
MIN_PAIR_SCORE = 0.4


class KnowledgeNotFound(LookupError):
    """The instruction, pair or proposal does not exist in this project."""


class KnowledgeConflict(Exception):
    """The pair is not in the state the action needs (for example already decided)."""


class PairRejected(Exception):
    """The pair's SQL does not compile against the deployed semantic model."""

    def __init__(self, messages: Sequence[str]) -> None:
        """Join the compile error messages into the text and keep them as a tuple."""
        super().__init__("; ".join(messages))
        self.messages = tuple(messages)


def check_pair_sql(logical_sql: str, mdl: Mdl) -> None:
    """Structural compile with no row policies: only logical names, one SELECT."""
    compiled = compile_query(logical_sql, mdl, None)
    if compiled.errors:
        raise PairRejected([error.message for error in compiled.errors])


class KnowledgeService:
    """Manages instructions and SQL pairs, keeping the vector index in step with the repository."""
    def __init__(
        self, repo: KnowledgeRepoPort, embedder: EmbedderPort, vector_store: VectorStorePort
    ) -> None:
        """Store the repository, embedder and vector store."""
        self._repo = repo
        self._embedder = embedder
        self._store = vector_store

    # ------------------------------------------------------------------ lookup

    def lookup(self, project_id: str, vector: Sequence[float]) -> KnowledgeContext:
        """The approved pairs and active instructions closest to the question vector."""
        return KnowledgeContext(
            pairs=self._nearest_pairs(project_id, vector, PAIR_LIMIT),
            instructions=self._instructions(project_id, vector),
        )

    def _nearest_pairs(
        self, project_id: str, vector: Sequence[float], limit: int
    ) -> tuple[SqlPair, ...]:
        """Approved pairs near the vector, re-read from the repository and cut to ``limit``."""
        matches = self._store.search(project_id, PAIRS_NAMESPACE, vector, limit=limit * 3)
        ids = [m.record_id for m in matches if m.score >= MIN_PAIR_SCORE]
        found = {p.id: p for p in self._repo.get_pairs(project_id, ids, status="approved")}
        return tuple(found[i] for i in ids if i in found)[:limit]

    def _instructions(self, project_id: str, vector: Sequence[float]) -> tuple[Instruction, ...]:
        """All active instructions, or the best-scoring few when there are more than the limit."""
        active = self._repo.list_instructions(project_id, active_only=True)
        if len(active) <= INSTRUCTION_LIMIT:
            return tuple(active)
        scores = {
            m.record_id: m.score
            for m in self._store.search(
                project_id, INSTRUCTIONS_NAMESPACE, vector, limit=INSTRUCTION_LIMIT * 3
            )
        }
        ranked = sorted(active, key=lambda i: scores.get(i.id, -1.0), reverse=True)
        return tuple(ranked[:INSTRUCTION_LIMIT])

    # ------------------------------------------------------------ instructions

    def list_instructions(self, project_id: str) -> list[Instruction]:
        """List the project's instructions."""
        return self._repo.list_instructions(project_id)

    def create_instruction(
        self, project_id: str, text: str, *, active: bool, actor_id: str
    ) -> Instruction:
        """Index and store a new instruction with an audit event."""
        instruction = Instruction(
            str(uuid.uuid4()), project_id, text.strip(), active, actor_id, datetime.now(UTC)
        )
        self._index(project_id, INSTRUCTIONS_NAMESPACE, instruction.id, instruction.text, "instruction")
        return self._repo.create_instruction(
            instruction,
            audit=_audit(actor_id, "instruction_created", "instruction", instruction.id, project_id,
                         {"text": instruction.text[:200], "active": active}),
        )

    def update_instruction(
        self,
        project_id: str,
        instruction_id: str,
        *,
        text: str | None,
        active: bool | None,
        actor_id: str,
    ) -> Instruction:
        """Change an instruction's text and/or active flag, re-indexing new text."""
        text = text.strip() if text is not None else None
        if self._repo.get_instruction(project_id, instruction_id) is None:
            raise KnowledgeNotFound("Instruction not found")
        if text is not None:
            self._index(project_id, INSTRUCTIONS_NAMESPACE, instruction_id, text, "instruction")
        updated = self._repo.update_instruction(
            project_id, instruction_id, text=text, active=active,
            audit=_audit(actor_id, "instruction_updated", "instruction", instruction_id,
                         project_id, {"text": (text or "")[:200], "active": active}),
        )
        if updated is None:
            raise KnowledgeNotFound("Instruction not found")
        return updated

    def delete_instruction(self, project_id: str, instruction_id: str, *, actor_id: str) -> None:
        """Delete an instruction and drop it from the index."""
        deleted = self._repo.delete_instruction(
            project_id, instruction_id,
            audit=_audit(actor_id, "instruction_deleted", "instruction", instruction_id,
                         project_id, {}),
        )
        if not deleted:
            raise KnowledgeNotFound("Instruction not found")
        self._unindex(project_id, INSTRUCTIONS_NAMESPACE, instruction_id)

    # ------------------------------------------------------------------- pairs

    def list_pairs(self, project_id: str, *, status: PairStatus | None = "approved") -> list[SqlPair]:
        """List the project's SQL pairs, by default only approved ones."""
        return self._repo.list_pairs(project_id, status=status)

    def create_pair(
        self, project_id: str, question: str, logical_sql: str, *, actor_id: str, mdl: Mdl
    ) -> SqlPair:
        """A pair added by hand is approved on the spot, so it needs the approval capability."""
        question, logical_sql = question.strip(), logical_sql.strip()
        check_pair_sql(logical_sql, mdl)
        if self._repo.find_duplicate(project_id, question, logical_sql) is not None:
            raise KnowledgeConflict("This question and SQL are already saved")
        now = datetime.now(UTC)
        pair = SqlPair(
            str(uuid.uuid4()), project_id, question, logical_sql, "approved", "manual",
            actor_id, now, actor_id, now,
        )
        self._index(project_id, PAIRS_NAMESPACE, pair.id, question, "sql_pair")
        return self._repo.create_pair(
            pair,
            audit=_audit(actor_id, "sql_pair_created", "sql_pair", pair.id, project_id,
                         {"question": question[:200], "status": "approved"}),
        )

    def delete_pair(self, project_id: str, pair_id: str, *, actor_id: str) -> None:
        """Delete a SQL pair and drop it from the index."""
        deleted = self._repo.delete_pair(
            project_id, pair_id,
            audit=_audit(actor_id, "sql_pair_deleted", "sql_pair", pair_id, project_id, {}),
        )
        if not deleted:
            raise KnowledgeNotFound("SQL pair not found")
        self._unindex(project_id, PAIRS_NAMESPACE, pair_id)

    # --------------------------------------------------------------- proposals

    def submit_feedback(
        self,
        project_id: str,
        *,
        user_id: str,
        rating: Rating,
        question: str,
        logical_sql: str,
        thread_id: str | None,
        message_id: str | None,
        mdl: Mdl,
    ) -> tuple[Feedback, SqlPair | None]:
        """Thumbs-down is recorded; thumbs-up also proposes the pair for review."""
        question, logical_sql = question.strip(), logical_sql.strip()
        check_pair_sql(logical_sql, mdl)
        feedback = Feedback(
            str(uuid.uuid4()), project_id, user_id, rating, question, logical_sql,
            thread_id, message_id,
        )
        audits = [
            _audit(user_id, "feedback_given", "feedback", feedback.id, project_id,
                   {"rating": rating, "thread_id": thread_id, "message_id": message_id})
        ]
        if rating == "down":
            self._repo.record_feedback(feedback, pair=None, pair_id=None, audits=audits)
            return feedback, None
        existing = self._repo.find_duplicate(project_id, question, logical_sql)
        if existing is not None:
            self._repo.record_feedback(feedback, pair=None, pair_id=existing.id, audits=audits)
            return feedback, existing
        pair = SqlPair(
            str(uuid.uuid4()), project_id, question, logical_sql, "proposed", "feedback",
            user_id, datetime.now(UTC),
        )
        audits.append(
            _audit(user_id, "sql_pair_proposed", "sql_pair", pair.id, project_id,
                   {"question": question[:200], "feedback_id": feedback.id})
        )
        self._repo.record_feedback(feedback, pair=pair, pair_id=None, audits=audits)
        return feedback, pair

    def list_proposals(self, project_id: str) -> list[tuple[SqlPair, SqlPair | None]]:
        """Each proposal with the nearest approved pair, so a reviewer can spot near-duplicates."""
        proposals = self._repo.list_pairs(project_id, status="proposed")
        if not proposals:
            return []
        vectors = self._embedder.embed([p.question for p in proposals])
        if len(vectors) != len(proposals):
            raise ValueError("Embedder must return one vector per question")
        return [
            (proposal, next(iter(self._nearest_pairs(project_id, vector, 1)), None))
            for proposal, vector in zip(proposals, vectors, strict=True)
        ]

    def approve(self, project_id: str, pair_id: str, *, actor_id: str, mdl: Mdl) -> SqlPair:
        """Re-check a proposal against the current model, index it and mark it approved."""
        pair = self._proposed(project_id, pair_id)
        check_pair_sql(pair.logical_sql, mdl)  # the model may have changed since it was proposed
        self._index(project_id, PAIRS_NAMESPACE, pair.id, pair.question, "sql_pair")
        decided = self._repo.decide_pair(
            project_id, pair_id, status="approved", actor_id=actor_id,
            audit=_audit(actor_id, "sql_pair_approved", "sql_pair", pair_id, project_id,
                         {"question": pair.question[:200], "proposed_by": pair.proposed_by}),
        )
        if decided is None:
            self._unindex(project_id, PAIRS_NAMESPACE, pair_id)
            raise KnowledgeConflict("The proposal was already decided")
        return decided

    def reject(self, project_id: str, pair_id: str, *, actor_id: str, reason: str = "") -> SqlPair:
        """Mark a proposal rejected, recording the reason in the audit event."""
        pair = self._proposed(project_id, pair_id)
        decided = self._repo.decide_pair(
            project_id, pair_id, status="rejected", actor_id=actor_id,
            audit=_audit(actor_id, "sql_pair_rejected", "sql_pair", pair_id, project_id,
                         {"question": pair.question[:200], "reason": reason[:500]}),
        )
        if decided is None:
            raise KnowledgeConflict("The proposal was already decided")
        return decided

    def _proposed(self, project_id: str, pair_id: str) -> SqlPair:
        """Load a pair that is still awaiting a decision, or raise."""
        pair = self._repo.get_pair(project_id, pair_id)
        if pair is None:
            raise KnowledgeNotFound("Proposal not found")
        if pair.status != "proposed":
            raise KnowledgeConflict("The proposal was already decided")
        return pair

    # ------------------------------------------------------------------- index

    def _index(self, project_id: str, namespace: str, record_id: str, text: str, kind: str) -> None:
        """Embed the text and upsert it into the vector store under the namespace."""
        vectors = self._embedder.embed([text])
        if len(vectors) != 1:
            raise ValueError("Embedder must return exactly one vector")
        self._store.ensure_collection(len(vectors[0]))
        self._store.upsert(
            project_id, namespace,
            [VectorRecord(record_id, tuple(vectors[0]), {"kind": kind, "text": text})],
        )

    def _unindex(self, project_id: str, namespace: str, record_id: str) -> None:
        # Reads re-check the repository, so a point that cannot be deleted now is inert.
        """Best-effort removal from the vector store (a stale point is inert: reads re-check)."""
        with contextlib.suppress(Exception):
            self._store.delete_records(project_id, namespace, [record_id])


def _audit(
    actor_id: str, action: str, resource_type: str, resource_id: str, project_id: str,
    details: dict[str, object],
) -> AuditEvent:
    """Build an audit event with the project id added to its details."""
    return AuditEvent(
        actor_id, action, resource_type, resource_id, {"project_id": project_id, **details}
    )

