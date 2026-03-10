from openai import AsyncOpenAI
from ai.base import AIProvider

_DEFAULT_MODEL = "gpt-4o"


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
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system or "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ],
        )
        return response.choices[0].message.content
