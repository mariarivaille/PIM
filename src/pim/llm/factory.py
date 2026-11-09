from pim.config import Settings
from pim.llm.base import LLMProvider


def make_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "openrouter":
        from pim.llm.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(
            api_key=settings.openrouter_api_key,
            model=settings.openrouter_model,
            base_url=settings.openrouter_base_url,
        )
    if settings.llm_provider == "gigachat":
        from pim.llm.gigachat import GigaChatProvider

        return GigaChatProvider(
            credentials=settings.gigachat_credentials,
            access_token=settings.gigachat_access_token,
            base_url=settings.gigachat_base_url,
            scope=settings.gigachat_scope,
            model=settings.gigachat_model,
            verify_ssl=settings.gigachat_verify_ssl,
        )
    raise ValueError(f"unknown LLM provider: {settings.llm_provider}")
