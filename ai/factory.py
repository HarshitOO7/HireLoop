import os
from dotenv import load_dotenv
from ai.base import AIProvider

load_dotenv()


class AIFactory:
    """Creates AI provider instances from environment variables.

    Two slots:
      - fast: cheap/high-volume provider (parse_job, analyze_fit)
      - quality: best provider (tailor_resume, write_cover_letter)
    """

    @classmethod
    def create_fast(cls) -> AIProvider:
        return cls._build(
            provider=os.getenv("AI_FAST_PROVIDER", "groq"),
            api_key=os.getenv("AI_FAST_API_KEY", ""),
            model=os.getenv("AI_FAST_MODEL", ""),
        )

    @classmethod
    def create_quality(cls) -> AIProvider:
        return cls._build(
            provider=os.getenv("AI_QUALITY_PROVIDER", "anthropic"),
            api_key=os.getenv("AI_QUALITY_API_KEY", ""),
            model=os.getenv("AI_QUALITY_MODEL", ""),
        )

    @classmethod
    def _build(cls, provider: str, api_key: str, model: str) -> AIProvider:
        match provider.lower():
            case "anthropic":
                from ai.providers.anthropic_provider import AnthropicProvider
                return AnthropicProvider(api_key=api_key, model=model)
            case "openai":
                from ai.providers.openai_provider import OpenAIProvider
                return OpenAIProvider(api_key=api_key, model=model)
            case "groq":
                from ai.providers.groq_provider import GroqProvider
                return GroqProvider(api_key=api_key, model=model)
            case "gemini":
                from ai.providers.gemini_provider import GeminiProvider
                return GeminiProvider(api_key=api_key, model=model)
            case "ollama":
                from ai.providers.ollama_provider import OllamaProvider
                return OllamaProvider(model=model, host=os.getenv("OLLAMA_HOST", ""))
            case _:
                raise ValueError(
                    f"Unknown AI provider: '{provider}'. "
                    "Valid options: anthropic, openai, groq, gemini, ollama"
                )
