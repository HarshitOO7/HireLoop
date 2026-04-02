import asyncio
import json
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

    async def complete(self, prompt: str, system: str = "", max_tokens: int | None = None) -> str:
        _max = max_tokens or self._max_tokens
        logger.debug("[anthropic] → model=%s  prompt=%d chars  system=%d chars  max_tokens=%d",
                     self._model, len(prompt), len(system), _max)
        for attempt in range(4):
            try:
                t0 = time.monotonic()
                msg = await self._client.messages.create(
                    model=self._model,
                    max_tokens=_max,
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
            except anthropic.RateLimitError:
                if attempt == 3:
                    raise
                wait = 2 ** attempt
                logger.warning("[anthropic] 429 — retry %d in %ds", attempt + 1, wait)
                await asyncio.sleep(wait)

    async def complete_json(
        self,
        prompt: str,
        system: str = "",
        schema: dict | None = None,
        max_tokens: int | None = None,
    ) -> str:
        _max = max_tokens or self._max_tokens
        tool = {
            "name": "respond",
            "description": "Return the structured response",
            "input_schema": schema or {"type": "object"},
        }
        logger.debug("[anthropic/json] → model=%s  prompt=%d chars  max_tokens=%d",
                     self._model, len(prompt), _max)
        for attempt in range(4):
            try:
                t0 = time.monotonic()
                msg = await self._client.messages.create(
                    model=self._model,
                    max_tokens=_max,
                    system=system or "You are a helpful assistant.",
                    tools=[tool],
                    tool_choice={"type": "tool", "name": "respond"},
                    messages=[{"role": "user", "content": prompt}],
                )
                elapsed = time.monotonic() - t0
                for block in msg.content:
                    if block.type == "tool_use":
                        text = json.dumps(block.input)
                        logger.debug("[anthropic/json] ← %.2fs  response=%d chars", elapsed, len(text))
                        return text
                # Fallback — should never happen with forced tool_choice
                text = msg.content[0].text if msg.content else "{}"
                logger.warning("[anthropic/json] no tool_use block — falling back to text")
                return text
            except anthropic.RateLimitError:
                if attempt == 3:
                    raise
                wait = 2 ** attempt
                logger.warning("[anthropic/json] 429 — retry %d in %ds", attempt + 1, wait)
                await asyncio.sleep(wait)
