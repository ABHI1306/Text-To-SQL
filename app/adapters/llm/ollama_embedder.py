"""Ollama adapter that turns texts into embedding vectors."""

from collections.abc import Sequence

import httpx

from app.ports.embedder import EmbedderPort


class OllamaEmbedderAdapter(EmbedderPort):
    """Embeds texts with the local Ollama ``/api/embed`` endpoint."""
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_seconds: float = 120,
    ) -> None:
        """Validate and store the Ollama URL, embedding model and timeout."""
        if not base_url:
            raise ValueError("OLLAMA_URL is required")
        if not model:
            raise ValueError("EMBEDDING_MODEL is required")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """Embed ``texts``, checking count, numeric values and consistent dimensions."""
        if not texts:
            return []
        try:
            response = httpx.post(
                f"{self._base_url}/api/embed",
                json={"model": self._model, "input": list(texts), "truncate": False},
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as error:
            raise RuntimeError("Local Ollama embedding model is unavailable") from error
        embeddings = payload.get("embeddings") if isinstance(payload, dict) else None
        if (
            not isinstance(embeddings, list)
            or len(embeddings) != len(texts)
            or any(
                not isinstance(vector, list)
                or not vector
                or any(not isinstance(value, (int, float)) for value in vector)
                for vector in embeddings
            )
        ):
            raise TypeError("Local Ollama returned invalid embeddings")
        dimensions = len(embeddings[0])
        if any(len(vector) != dimensions for vector in embeddings):
            raise ValueError("Local Ollama returned embeddings with inconsistent dimensions")
        return [tuple(float(value) for value in vector) for vector in embeddings]
