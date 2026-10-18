"""Port for a byte-valued key/value cache with expiry."""

from typing import Protocol


class CachePort(Protocol):
    """Byte-valued key/value store with per-entry expiry."""

    def get(self, key: str) -> bytes | None:
        """Return the value stored under ``key``, or None when absent or expired."""
        ...

    def set(self, key: str, value: bytes, *, ttl_seconds: int) -> None:
        """Store ``value`` under ``key``, replacing any old one, for ``ttl_seconds``."""
        ...

    def delete(self, key: str) -> None:
        """Remove ``key``; deleting a missing key is not an error."""
        ...