"""Port for the local language model."""

from collections.abc import Mapping
from typing import Protocol


class LLMPort(Protocol):
    """Text generation backend."""
    def generate(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        schema: Mapping[str, object] | None = None,
    ) -> str:
        """Return the model's JSON text; ``schema`` constrains it where the backend supports it."""
        ...
