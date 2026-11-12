"""CodeVector LLM (OpenAI-compatible) gateway for the Telegram bot agent."""
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.config import settings

LLM_TIMEOUT = 25  # seconds per request: fail fast instead of typing forever


class LLMConfigError(RuntimeError):
    pass


def _clean(key: str) -> str:
    return key.strip().strip("\"'").strip()


def get_llm() -> BaseChatModel:
    key = _clean(settings.codevector_api_key)
    if not key:
        raise LLMConfigError("CODEVECTOR_API_KEY is empty. Put your CodeVector key in .env")

    model = settings.codevector_model or "dev-model-alpha"
    base_url = settings.codevector_base_url or "https://coding-gateway.fissionlabs.com/gateway/openai/v1"
    temp = settings.llm_temperature

    return ChatOpenAI(
        model=model,
        api_key=key,
        base_url=base_url,
        temperature=temp,
        timeout=LLM_TIMEOUT,
        max_retries=1,
    )
