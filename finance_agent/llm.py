"""
The one place this service talks to a language model (CodeVector, OpenAI-compatible). The model only ever *reads* text and
returns JSON; it never calculates an amount. Everything else takes an `LLM`, so tests can plug in a fake.
"""
import json
import logging
import re
from typing import Protocol

import httpx

from finance_agent.config import settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    async def json(self, system: str, user: str, *, timeout: int | None = None) -> dict: ...


def extract_json(text: str) -> dict:
    """The first JSON object in a model's reply (models like to wrap it in ``` fences or add a sentence)."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        value = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("the model did not return JSON") from None
        try:
            value = json.loads(text[start:end + 1])
        except ValueError:
            raise LLMError("the model returned broken JSON") from None
    if isinstance(value, list):  # a bare list of rules: keep it, the caller decides what it means
        return {"rules": value}
    if not isinstance(value, dict):
        raise LLMError("the model did not return a JSON object")
    return value


class CodeVectorLLM:
    def __init__(self, client: httpx.AsyncClient | None = None):
        key = settings.codevector_api_key.strip().strip("\"'")
        if not key:
            raise LLMError("CODEVECTOR_API_KEY is empty. Put your CodeVector key in .env")
        self._key = key
        self._client = client

    async def _complete(self, system: str, user: str, timeout: int | None = None) -> str:
        body = {
            "model": settings.codevector_model, "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        headers = {"Authorization": f"Bearer {self._key}"}
        url = settings.codevector_base_url.rstrip("/") + "/chat/completions"
        wait = timeout or settings.llm_timeout
        try:
            if self._client is not None:
                r = await self._client.post(url, json=body, headers=headers, timeout=wait)
            else:
                async with httpx.AsyncClient() as c:
                    r = await c.post(url, json=body, headers=headers, timeout=wait)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"] or ""
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            raise LLMError(f"the language model is not reachable ({type(e).__name__})") from e

    async def json(self, system: str, user: str, *, timeout: int | None = None) -> dict:
        reply = await self._complete(system + "\n\nReply with ONE JSON object and nothing else.", user, timeout)
        try:
            return extract_json(reply)
        except LLMError:  # one more try, asking again more firmly
            log.warning("the model's reply was not JSON; asking again. It said: %r", reply[:300])
            reply = await self._complete(system + "\n\nYour last reply was not valid JSON. Reply with ONLY one JSON object, no prose, no code fences.", user, timeout)
            try:
                return extract_json(reply)
            except LLMError:
                log.warning("the second reply was not JSON either: %r", reply[:300])
                raise


class LazyLLM:
    """The service starts (and the A2A endpoint works) even without a key; only a call that needs the model reports it."""

    def __init__(self, factory=CodeVectorLLM):
        self._factory, self._llm = factory, None

    async def json(self, system: str, user: str, *, timeout: int | None = None) -> dict:
        if self._llm is None:
            self._llm = self._factory()
        return await self._llm.json(system, user, timeout=timeout)
