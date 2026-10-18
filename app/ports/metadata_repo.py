"""Port for storing semantic-model versions and audit events in the metadata database."""

from typing import Protocol

from app.core.types import AuditEvent


class MetadataRepoPort(Protocol):
    """Metadata storage for MDL versions and audit events."""
    def get_mdl_version(
        self, project_id: str, version_id: str
    ) -> bytes | None:
        """Return the stored MDL document bytes, or None when the version does not exist."""
        ...

    def save_mdl_version(
        self, project_id: str, version_id: str, document: bytes
    ) -> None:
        """Store the serialized MDL ``document`` under this project and version."""
        ...

    def append_audit_event(self, event: AuditEvent) -> None:
        """Record one audit event."""
        ...