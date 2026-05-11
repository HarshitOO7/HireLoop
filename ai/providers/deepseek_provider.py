import logging
import time

from openai import AsyncOpenAI
from ai.base import AIProvider

_DEFAULT_MODEL = "deepseek-chat"
_BASE_URL = "https://api.deepseek.com"
logger = logging.getLogger(__name__)


class DeepSeekProvider(AIProvider):
    def __init__(self, api_key: str, model: str = ""):
        self._client = AsyncOpenAI(api_key=api_key, base_url=_BASE_URL)
        self._model = model or _DEFAULT_MODEL

    @property
    def provider_name(self) -> str:
        return "deepseek"

    async def complete(self, prompt: str, system: str = "", max_tokens: int | None = None) -> str:
        kwargs = {}
        if max_tokens:
            kwargs["max_completion_tokens"] = max_tokens
        logger.debug("[deepseek] → model=%s  prompt=%d chars  system=%d chars",
                     self._model, len(prompt), len(system))
        t0 = time.monotonic()
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system or "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ],
            **kwargs,
        )
        elapsed = time.monotonic() - t0
        text = response.choices[0].message.content
        usage = response.usage
        cache_hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
        logger.debug("[deepseek] ← %.2fs  %d chars  in=%s(+%s cached) out=%s",
                     elapsed, len(text),
                     getattr(usage, "prompt_tokens", "?"), cache_hit,
                     getattr(usage, "completion_tokens", "?"))
        return text

    async def complete_json(
        self,
        prompt: str,
        system: str = "",
        schema: dict | None = None,
        max_tokens: int | None = None,
    ) -> str:
        kwargs: dict = {"response_format": {"type": "json_object"}}
        if max_tokens:
            kwargs["max_completion_tokens"] = max_tokens
        logger.debug("[deepseek/json] → model=%s  prompt=%d chars", self._model, len(prompt))
        t0 = time.monotonic()
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system or "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ],
            **kwargs,
        )
        elapsed = time.monotonic() - t0
        text = response.choices[0].message.content
        usage = response.usage
        cache_hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
        logger.debug("[deepseek/json] ← %.2fs  %d chars  in=%s(+%s cached) out=%s",
                     elapsed, len(text),
                     getattr(usage, "prompt_tokens", "?"), cache_hit,
                     getattr(usage, "completion_tokens", "?"))
        return text
