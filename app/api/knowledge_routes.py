"""Day 6 knowledge routes: instructions, approved SQL pairs, feedback and the proposal queue."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.agent.factory import NoDeployedModel, load_deployed_mdl
from app.api.dependencies import (
    get_knowledge_service,
    get_repository,
    require_capability,
    require_project_access,
)
from app.auth.models import AuthContext
from app.domain.mdl import Mdl
from app.knowledge.service import (
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeService,
    PairRejected,
)
from app.ports.knowledge import Instruction, SqlPair

router = APIRouter(prefix="/api/v1")

RepositoryDependency = Annotated[PostgresMetadataRepository, Depends(get_repository)]
KnowledgeDependency = Annotated[KnowledgeService, Depends(get_knowledge_service)]
ManageKnowledgeContext = Annotated[AuthContext, Depends(require_capability("manage_knowledge"))]
ApprovePairsContext = Annotated[AuthContext, Depends(require_capability("approve_pairs"))]
GiveFeedbackContext = Annotated[AuthContext, Depends(require_capability("give_feedback"))]


class InstructionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1000, pattern=r"\S")
    active: bool = True


class InstructionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, min_length=1, max_length=1000, pattern=r"\S")
    active: bool | None = None

    @model_validator(mode="after")
    def _something(self) -> InstructionPatch:
        if self.text is None and self.active is None:
            raise ValueError("Give text or active")
        return self


class SqlPairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    logical_sql: str = Field(min_length=1, max_length=20_000, pattern=r"\S")


class FeedbackRequest(SqlPairRequest):
    message_id: str | None = Field(default=None, min_length=1, max_length=100)
    thread_id: UUID | None = None
    rating: Literal["up", "down"]

    @model_validator(mode="after")
    def _anchored(self) -> FeedbackRequest:
        if self.message_id is None and self.thread_id is None:
            raise ValueError("Give message_id or thread_id")
        return self


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=500)


def _instruction_json(item: Instruction) -> dict[str, object]:
    return {
        "id": item.id,
        "text": item.text,
        "active": item.active,
        "created_by": item.created_by,
        "updated_at": item.updated_at.isoformat(),
    }


def _pair_json(pair: SqlPair) -> dict[str, object]:
    return {
        "id": pair.id,
        "question": pair.question,
        "logical_sql": pair.logical_sql,
        "status": pair.status,
        "source": pair.source,
        "proposed_by": pair.proposed_by,
        "decided_by": pair.decided_by,
        "created_at": pair.created_at.isoformat(),
        "decided_at": pair.decided_at.isoformat() if pair.decided_at else None,
    }


@contextmanager
def _mapped() -> Iterator[None]:
    """Typed knowledge errors become HTTP errors; infrastructure trouble is a 503, never a 500."""
    try:
        yield
    except HTTPException:
        raise
    except KnowledgeNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except KnowledgeConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except PairRejected as error:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_SQL", "message": str(error), "messages": list(error.messages)},
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=503, detail="The embedding model or vector store is unavailable"
        ) from error


def _deployed_mdl(repository: PostgresMetadataRepository, project_id: UUID) -> Mdl:
    try:
        return load_deployed_mdl(repository, str(project_id))[1]
    except NoDeployedModel as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


# ------------------------------------------------------------------ instructions


@router.get("/projects/{project_id}/knowledge/instructions")
def list_instructions(
    project_id: UUID, context: ManageKnowledgeContext, knowledge: KnowledgeDependency
) -> list[dict[str, object]]:
    require_project_access(context, project_id)
    with _mapped():
        return [_instruction_json(i) for i in knowledge.list_instructions(str(project_id))]


@router.post("/projects/{project_id}/knowledge/instructions", status_code=201)
def create_instruction(
    project_id: UUID,
    body: InstructionRequest,
    context: ManageKnowledgeContext,
    knowledge: KnowledgeDependency,
) -> dict[str, object]:
    require_project_access(context, project_id)
    with _mapped():
        return _instruction_json(
            knowledge.create_instruction(
                str(project_id), body.text, active=body.active, actor_id=context.user_id
            )
        )


@router.patch("/projects/{project_id}/knowledge/instructions/{instruction_id}")
def update_instruction(
    project_id: UUID,
    instruction_id: UUID,
    body: InstructionPatch,
    context: ManageKnowledgeContext,
    knowledge: KnowledgeDependency,
) -> dict[str, object]:
    require_project_access(context, project_id)
    with _mapped():
        return _instruction_json(
            knowledge.update_instruction(
                str(project_id), str(instruction_id), text=body.text, active=body.active,
                actor_id=context.user_id,
            )
        )


@router.delete(
    "/projects/{project_id}/knowledge/instructions/{instruction_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_instruction(
    project_id: UUID,
    instruction_id: UUID,
    context: ManageKnowledgeContext,
    knowledge: KnowledgeDependency,
) -> Response:
    require_project_access(context, project_id)
    with _mapped():
        knowledge.delete_instruction(str(project_id), str(instruction_id), actor_id=context.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------- sql pairs


@router.get("/projects/{project_id}/knowledge/sql-pairs")
def list_sql_pairs(
    project_id: UUID, context: ManageKnowledgeContext, knowledge: KnowledgeDependency
) -> list[dict[str, object]]:
    require_project_access(context, project_id)
    with _mapped():
        return [_pair_json(p) for p in knowledge.list_pairs(str(project_id), status="approved")]


@router.post("/projects/{project_id}/knowledge/sql-pairs", status_code=201)
def create_sql_pair(
    project_id: UUID,
    body: SqlPairRequest,
    context: ApprovePairsContext,
    repository: RepositoryDependency,
    knowledge: KnowledgeDependency,
) -> dict[str, object]:
    """Adding a pair by hand approves it, so it needs ``approve_pairs``."""
    require_project_access(context, project_id)
    mdl = _deployed_mdl(repository, project_id)
    with _mapped():
        return _pair_json(
            knowledge.create_pair(
                str(project_id), body.question, body.logical_sql, actor_id=context.user_id, mdl=mdl
            )
        )


@router.delete(
    "/projects/{project_id}/knowledge/sql-pairs/{pair_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_sql_pair(
    project_id: UUID,
    pair_id: UUID,
    context: ManageKnowledgeContext,
    knowledge: KnowledgeDependency,
) -> Response:
    require_project_access(context, project_id)
    with _mapped():
        knowledge.delete_pair(str(project_id), str(pair_id), actor_id=context.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------- feedback, proposals


@router.post("/projects/{project_id}/feedback", status_code=201)
def give_feedback(
    project_id: UUID,
    body: FeedbackRequest,
    context: GiveFeedbackContext,
    repository: RepositoryDependency,
    knowledge: KnowledgeDependency,
) -> dict[str, object]:
    """Thumbs-up proposes the pair for review; thumbs-down is only recorded."""
    require_project_access(context, project_id)
    mdl = _deployed_mdl(repository, project_id)
    with _mapped():
        feedback, pair = knowledge.submit_feedback(
            str(project_id),
            user_id=context.user_id,
            rating=body.rating,
            question=body.question,
            logical_sql=body.logical_sql,
            thread_id=str(body.thread_id) if body.thread_id else None,
            message_id=body.message_id,
            mdl=mdl,
        )
    return {
        "id": feedback.id,
        "rating": feedback.rating,
        "proposal": _pair_json(pair) if pair else None,
    }


@router.get("/projects/{project_id}/knowledge/proposals")
def list_proposals(
    project_id: UUID, context: ApprovePairsContext, knowledge: KnowledgeDependency
) -> list[dict[str, object]]:
    require_project_access(context, project_id)
    with _mapped():
        return [
            {**_pair_json(proposal), "nearest_approved": _pair_json(nearest) if nearest else None}
            for proposal, nearest in knowledge.list_proposals(str(project_id))
        ]


@router.post("/projects/{project_id}/knowledge/proposals/{pair_id}/approve")
def approve_proposal(
    project_id: UUID,
    pair_id: UUID,
    context: ApprovePairsContext,
    repository: RepositoryDependency,
    knowledge: KnowledgeDependency,
) -> dict[str, object]:
    require_project_access(context, project_id)
    mdl = _deployed_mdl(repository, project_id)
    with _mapped():
        return _pair_json(
            knowledge.approve(str(project_id), str(pair_id), actor_id=context.user_id, mdl=mdl)
        )


@router.post("/projects/{project_id}/knowledge/proposals/{pair_id}/reject")
def reject_proposal(
    project_id: UUID,
    pair_id: UUID,
    body: RejectRequest,
    context: ApprovePairsContext,
    knowledge: KnowledgeDependency,
) -> dict[str, object]:
    require_project_access(context, project_id)
    with _mapped():
        return _pair_json(
            knowledge.reject(
                str(project_id), str(pair_id), actor_id=context.user_id, reason=body.reason
            )
        )
