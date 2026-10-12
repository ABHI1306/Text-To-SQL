from typing import Protocol


class LLMPort(Protocol):
    def generate(self, prompt: str, *, temperature: float = 0.0) -> str: ...