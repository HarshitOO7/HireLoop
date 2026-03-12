import logging
import time

from openai import AsyncOpenAI
from ai.base import AIProvider

_DEFAULT_MODEL = "gpt-4o"
logger = logging.getLogger(__name__)


class OpenAIProvider(AIProvider):
    def __init__(self, api_key: str, model: str = "", base_url: str = ""):
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncOpenAI(**kwargs)
        self._model = model or _DEFAULT_MODEL

    @property
    def provider_name(self) -> str:
        return "openai"

    async def complete(self, prompt: str, system: str = "") -> str:
        logger.debug("[openai] → model=%s  prompt=%d chars  system=%d chars",
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
        logger.debug("[openai] ← %.2fs  response=%d chars  tokens in=%s out=%s total=%s",
                     elapsed, len(text),
                     getattr(usage, "prompt_tokens", "?"),
                     getattr(usage, "completion_tokens", "?"),
                     getattr(usage, "total_tokens", "?"))
        return text
