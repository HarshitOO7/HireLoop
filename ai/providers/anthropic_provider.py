import logging
import time

import anthropic
from ai.base import AIProvider

_DEFAULT_MODEL = "claude-sonnet-4-6"
logger = logging.getLogger(__name__)


class AnthropicProvider(AIProvider):
    def __init__(self, api_key: str, model: str = ""):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model or _DEFAULT_MODEL
        self._max_tokens = 4096

    @property
    def provider_name(self) -> str:
        return "anthropic"

    async def complete(self, prompt: str, system: str = "") -> str:
        logger.debug("[anthropic] → model=%s  prompt=%d chars  system=%d chars  max_tokens=%d",
                     self._model, len(prompt), len(system), self._max_tokens)
        t0 = time.monotonic()
        msg = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system or "You are a helpful assistant.",
            messages=[{"role": "user", "content": prompt}],
        )
        elapsed = time.monotonic() - t0
        text = msg.content[0].text
        usage = msg.usage
        logger.debug("[anthropic] ← %.2fs  response=%d chars  tokens in=%s out=%s",
                     elapsed, len(text),
                     getattr(usage, "input_tokens", "?"),
                     getattr(usage, "output_tokens", "?"))
        return text
