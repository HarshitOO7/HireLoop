import logging
import time

from groq import AsyncGroq
from ai.base import AIProvider

_DEFAULT_MODEL = "llama-3.3-70b-versatile"
logger = logging.getLogger(__name__)


class GroqProvider(AIProvider):
    def __init__(self, api_key: str, model: str = ""):
        self._client = AsyncGroq(api_key=api_key)
        self._model = model or _DEFAULT_MODEL

    @property
    def provider_name(self) -> str:
        return "groq"

    async def complete(self, prompt: str, system: str = "") -> str:
        logger.debug("[groq] → model=%s  prompt=%d chars  system=%d chars",
                     self._model, len(prompt), len(system))
        t0 = time.monotonic()
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system or "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ],
        )
        elapsed = time.monotonic() - t0
        text = response.choices[0].message.content
        usage = response.usage
        logger.debug("[groq] ← %.2fs  response=%d chars  tokens in=%s out=%s total=%s",
                     elapsed, len(text),
                     getattr(usage, "prompt_tokens", "?"),
                     getattr(usage, "completion_tokens", "?"),
                     getattr(usage, "total_tokens", "?"))
        return text
