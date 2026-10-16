"""The scheduler handler for 'saved_question' jobs."""

from __future__ import annotations

from collections.abc import Mapping

from app.collab.service import CollabService
from app.scheduler.runner import JobContext


class SavedQuestionJobHandler:
    """Runs one slot as the job's owner (the runner re-loaded and re-checked them just now)."""

    def __init__(self, service: CollabService) -> None:
        self._service = service

    def __call__(self, context: JobContext) -> Mapping[str, object]:
        return self._service.execute_scheduled(context.job, context.slot, context.owner)
