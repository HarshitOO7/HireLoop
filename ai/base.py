from abc import ABC, abstractmethod


class AIProvider(ABC):
    """Abstract base for all AI providers. Implement complete() and provider_name."""

    @abstractmethod
    async def complete(self, prompt: str, system: str = "") -> str:
        """Send a prompt and return the text response."""
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable provider name, e.g. 'anthropic'."""
        ...
