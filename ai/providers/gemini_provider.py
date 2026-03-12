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

    async def complete(self, prompt: str, system: str = "") -> str:
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        logger.debug("[gemini] → model=%s  full_prompt=%d chars", self._model_name, len(full_prompt))
        t0 = time.monotonic()
        response = await self._model.generate_content_async(full_prompt)
        elapsed = time.monotonic() - t0
        text = response.text
        logger.debug("[gemini] ← %.2fs  response=%d chars", elapsed, len(text))
        return text
