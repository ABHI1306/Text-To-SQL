from collections.abc import Mapping

import httpx


class OllamaLLMAdapter:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_seconds: float = 120,
    ) -> None:
        if not base_url:
            raise ValueError("OLLAMA_URL is required")
        if not model:
            raise ValueError("SQL_MODEL is required")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds

    def generate(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        schema: Mapping[str, object] | None = None,
    ) -> str:
        try:
            response = httpx.post(
                f"{self._base_url}/api/generate",
                json={
                    "model": self._model,
                    "prompt": prompt,
                    "format": dict(schema) if schema is not None else "json",
                    "stream": False,
                    "options": {"temperature": temperature},
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as error:
            raise RuntimeError("Local Ollama model is unavailable") from error
        generated_text = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(generated_text, str):
            raise TypeError("Local Ollama returned an invalid response")
        return generated_text
