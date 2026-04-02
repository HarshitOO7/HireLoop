from abc import ABC, abstractmethod


class AIProvider(ABC):
    """Abstract base for all AI providers. Implement complete(), complete_json(), and provider_name."""

    @abstractmethod
    async def complete(self, prompt: str, system: str = "", max_tokens: int | None = None) -> str:
        """Send a prompt and return the text response."""
        ...

    @abstractmethod
    async def complete_json(
        self,
        prompt: str,
        system: str = "",
        schema: dict | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Like complete() but activates the provider's native JSON output mode.
        schema: optional JSON Schema dict for providers that support constrained decoding.
        Returns raw text — caller parses with _parse_json() as last-resort fallback.
        """
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable provider name, e.g. 'anthropic'."""
        ...
