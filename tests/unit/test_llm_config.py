"""CodeVector is the only LLM gateway; its config must be explicit."""
import pytest

from backend.agent.llm import LLMConfigError, get_llm
from backend.config import settings


def test_codevector_api_key_is_required(monkeypatch):
    monkeypatch.setattr(settings, "codevector_api_key", "")
    monkeypatch.setattr(settings, "codevector_base_url", "https://gateway.example/gateway/openai/v1")
    monkeypatch.setattr(settings, "codevector_model", "dev-model-alpha")

    with pytest.raises(LLMConfigError, match="CODEVECTOR_API_KEY"):
        get_llm()


def test_codevector_base_url_is_required(monkeypatch):
    monkeypatch.setattr(settings, "codevector_api_key", "cvg_test")
    monkeypatch.setattr(settings, "codevector_base_url", "")
    monkeypatch.setattr(settings, "codevector_model", "dev-model-alpha")

    with pytest.raises(LLMConfigError, match="CODEVECTOR_BASE_URL"):
        get_llm()


def test_codevector_model_is_required(monkeypatch):
    monkeypatch.setattr(settings, "codevector_api_key", "cvg_test")
    monkeypatch.setattr(settings, "codevector_base_url", "https://gateway.example/gateway/openai/v1")
    monkeypatch.setattr(settings, "codevector_model", "")

    with pytest.raises(LLMConfigError, match="CODEVECTOR_MODEL"):
        get_llm()


def test_get_llm_builds_codevector_client(monkeypatch):
    monkeypatch.setattr(settings, "codevector_api_key", "cvg_test")
    monkeypatch.setattr(settings, "codevector_base_url", "https://gateway.example/gateway/openai/v1/")
    monkeypatch.setattr(settings, "codevector_model", "dev-model-alpha")
    monkeypatch.setattr(settings, "llm_temperature", 0.0)

    llm = get_llm()

    assert llm.model_name == "dev-model-alpha"
    assert llm.openai_api_base == "https://gateway.example/gateway/openai/v1"
