import os
import httpx
from ai.base import AIProvider

_DEFAULT_MODEL = "llama3.2"


class OllamaProvider(AIProvider):
    def __init__(self, model: str = "", host: str = ""):
        self._model = model or _DEFAULT_MODEL
        self._host = host or os.getenv("OLLAMA_HOST", "http://localhost:11434")

    @property
    def provider_name(self) -> str:
        return "ollama"

    async def complete(self, prompt: str, system: str = "") -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{self._host}/api/chat",
                json={"model": self._model, "messages": messages, "stream": False},
            )
            r.raise_for_status()
            return r.json()["message"]["content"]
