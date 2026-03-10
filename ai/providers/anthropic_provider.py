import anthropic
from ai.base import AIProvider

_DEFAULT_MODEL = "claude-sonnet-4-6"


class AnthropicProvider(AIProvider):
    def __init__(self, api_key: str, model: str = ""):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model or _DEFAULT_MODEL
        self._max_tokens = 4096

    @property
    def provider_name(self) -> str:
        return "anthropic"

    async def complete(self, prompt: str, system: str = "") -> str:
        msg = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system or "You are a helpful assistant.",
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text
