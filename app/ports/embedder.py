from collections.abc import Sequence
from typing import Protocol


class EmbedderPort(Protocol):
    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]: ...