"""Build the chat model: CodeVector is the only supported LLM gateway."""
from langchain_core.language_models.chat_models import BaseChatModel

from backend.config import settings

LLM_TIMEOUT = 25  # seconds per request: fail fast instead of typing forever


class LLMConfigError(RuntimeError):
    pass


def _clean(value: str) -> str:
    return value.strip().strip("\"'").strip()


def get_llm() -> BaseChatModel:
    key = _clean(settings.codevector_api_key)
    if not key:
        raise LLMConfigError("CODEVECTOR_API_KEY is empty. Put your CodeVector key in .env")

    base_url = _clean(settings.codevector_base_url).rstrip("/")
    if not base_url:
        raise LLMConfigError(
            "CODEVECTOR_BASE_URL is empty. Set it to the gateway's OpenAI-compatible "
            "base URL (e.g. https://<your-gateway>/gateway/openai/v1)."
        )

    model = _clean(settings.codevector_model)
    if not model:
        raise LLMConfigError(
            "CODEVECTOR_MODEL is empty. Set it to the CodeVector gateway model slug "
            "(e.g. dev-model-alpha)."
        )

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model,
        api_key=key,
        base_url=base_url,
        temperature=settings.llm_temperature,
        timeout=LLM_TIMEOUT,
        max_retries=1,
    )
