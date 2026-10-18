"""Port for turning text into embedding vectors."""

from collections.abc import Sequence
from typing import Protocol


class EmbedderPort(Protocol):
    """Converts text to embedding vectors for retrieval."""

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """Return one vector per input text, in the same order and all of one size."""
        ...
