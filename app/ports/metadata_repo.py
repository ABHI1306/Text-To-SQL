from typing import Protocol

from app.core.types import AuditEvent


class MetadataRepoPort(Protocol):
    def get_mdl_version(
        self, project_id: str, version_id: str
    ) -> bytes | None: ...

    def save_mdl_version(
        self, project_id: str, version_id: str, document: bytes
    ) -> None: ...

    def append_audit_event(self, event: AuditEvent) -> None: ...