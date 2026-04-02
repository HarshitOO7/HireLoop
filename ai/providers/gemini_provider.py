import logging
import time

import google.generativeai as genai
from ai.base import AIProvider

_DEFAULT_MODEL = "gemini-2.0-flash"
logger = logging.getLogger(__name__)


class GeminiProvider(AIProvider):
    def __init__(self, api_key: str, model: str = ""):
        genai.configure(api_key=api_key)
        self._model_name = model or _DEFAULT_MODEL
        self._model = genai.GenerativeModel(self._model_name)

    @property
    def provider_name(self) -> str:
        return "gemini"

    async def complete(self, prompt: str, system: str = "", max_tokens: int | None = None) -> str:
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        config_kwargs = {}
        if max_tokens:
            config_kwargs["max_output_tokens"] = max_tokens
        logger.debug("[gemini] → model=%s  full_prompt=%d chars", self._model_name, len(full_prompt))
        t0 = time.monotonic()
        kwargs = {}
        if config_kwargs:
            kwargs["generation_config"] = genai.GenerationConfig(**config_kwargs)
        response = await self._model.generate_content_async(full_prompt, **kwargs)
        elapsed = time.monotonic() - t0
        text = response.text
        logger.debug("[gemini] ← %.2fs  response=%d chars", elapsed, len(text))
        return text

    async def complete_json(
        self,
        prompt: str,
        system: str = "",
        schema: dict | None = None,
        max_tokens: int | None = None,
    ) -> str:
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        config_kwargs: dict = {"response_mime_type": "application/json"}
        if max_tokens:
            config_kwargs["max_output_tokens"] = max_tokens
        logger.debug("[gemini/json] → model=%s  full_prompt=%d chars", self._model_name, len(full_prompt))
        t0 = time.monotonic()
        response = await self._model.generate_content_async(
            full_prompt,
            generation_config=genai.GenerationConfig(**config_kwargs),
        )
        elapsed = time.monotonic() - t0
        text = response.text
        logger.debug("[gemini/json] ← %.2fs  response=%d chars", elapsed, len(text))
        return text
