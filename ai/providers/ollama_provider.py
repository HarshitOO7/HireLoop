import logging
import os
import time

import httpx
from ai.base import AIProvider

_DEFAULT_MODEL = "llama3.2"
logger = logging.getLogger(__name__)


class OllamaProvider(AIProvider):
    def __init__(self, model: str = "", host: str = ""):
        self._model = model or _DEFAULT_MODEL
        self._host = host or os.getenv("OLLAMA_HOST", "http://localhost:11434")

    @property
    def provider_name(self) -> str:
        return "ollama"

    async def complete(self, prompt: str, system: str = "", max_tokens: int | None = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body: dict = {"model": self._model, "messages": messages, "stream": False}
        if max_tokens:
            body["options"] = {"num_predict": max_tokens}

        logger.debug("[ollama] → model=%s  host=%s  prompt=%d chars  system=%d chars",
                     self._model, self._host, len(prompt), len(system))
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"{self._host}/api/chat", json=body)
            r.raise_for_status()
            data = r.json()
        elapsed = time.monotonic() - t0
        text = data["message"]["content"]
        eval_count = data.get("eval_count", "?")
        prompt_eval_count = data.get("prompt_eval_count", "?")
        logger.debug("[ollama] ← %.2fs  response=%d chars  tokens in=%s out=%s",
                     elapsed, len(text), prompt_eval_count, eval_count)
        return text

    async def complete_json(
        self,
        prompt: str,
        system: str = "",
        schema: dict | None = None,
        max_tokens: int | None = None,
    ) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body: dict = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "format": "json",
        }
        if max_tokens:
            body["options"] = {"num_predict": max_tokens}

        logger.debug("[ollama/json] → model=%s  prompt=%d chars", self._model, len(prompt))
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"{self._host}/api/chat", json=body)
            r.raise_for_status()
            data = r.json()
        elapsed = time.monotonic() - t0
        text = data["message"]["content"]
        logger.debug("[ollama/json] ← %.2fs  response=%d chars", elapsed, len(text))
        return text
