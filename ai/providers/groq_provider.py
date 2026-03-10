from groq import AsyncGroq
from ai.base import AIProvider

_DEFAULT_MODEL = "llama-3.3-70b-versatile"


class GroqProvider(AIProvider):
    def __init__(self, api_key: str, model: str = ""):
        self._client = AsyncGroq(api_key=api_key)
        self._model = model or _DEFAULT_MODEL

    @property
    def provider_name(self) -> str:
        return "groq"

    async def complete(self, prompt: str, system: str = "") -> str:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system or "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ],
        )
        return response.choices[0].message.content
