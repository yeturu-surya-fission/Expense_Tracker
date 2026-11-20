"""
Our side of the conversation with the Finance Agent (the A2A protocol).

For every call we sign a short-lived token with the same RS256 key that signs our own tokens (the Finance Agent checks it
against our public JWKS), open the agent's card, send one message, and read back the task: its state (completed /
input-required / failed), the text for the human, and the structured data.
"""

import time
import uuid
from dataclasses import dataclass, field

import httpx
import jwt
from a2a.client import ClientConfig, ClientFactory
from a2a.helpers import get_data_parts, get_message_text, new_data_part, new_text_part
from a2a.types.a2a_pb2 import Message, Role, SendMessageRequest, TaskState

from mcp_server.auth.keys import get_keys
from mcp_server.config import settings

STATES = {
    TaskState.TASK_STATE_COMPLETED: "completed",
    TaskState.TASK_STATE_INPUT_REQUIRED: "input_required",
    TaskState.TASK_STATE_FAILED: "failed",
    TaskState.TASK_STATE_REJECTED: "failed",
    TaskState.TASK_STATE_CANCELED: "failed",
    TaskState.TASK_STATE_AUTH_REQUIRED: "failed",
}


class FinanceUnavailable(Exception):
    """The Finance Agent could not be reached or answered something we can't use."""


@dataclass
class Reply:
    state: str  # completed | input_required | failed | working
    text: str = ""
    data: dict = field(default_factory=dict)
    task_id: str = ""
    context_id: str = ""


# tests point this at the in-process Finance Agent instead of the network
_transport: httpx.AsyncBaseTransport | None = None


def audience() -> str:
    return settings.finance_agent_url.rstrip("/") + "/a2a"


def mint_token(user_id: int, name: str) -> str:
    """Says to the Finance Agent: 'this request is for employee <id>'. Only we can sign it."""
    now = int(time.time())
    keys = get_keys().current
    claims = {
        "iss": settings.issuer,
        "aud": audience(),
        "sub": f"user:{user_id}",
        "name": (name or "Employee")[:128],
        "scope": "a2a",
        "iat": now,
        "nbf": now,
        "exp": now + settings.a2a_token_ttl_seconds,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(
        claims,
        keys.private_pem,
        algorithm="RS256",
        headers={"kid": keys.kid, "typ": "at+jwt"},
    )


async def call(
    user_id: int,
    name: str,
    skill: str,
    payload: dict,
    text: str,
    *,
    task_id: str | None = None,
    context_id: str | None = None,
) -> Reply:
    """Send one A2A message to the Finance Agent. Pass `task_id` to continue a task that is waiting for an answer."""
    http = httpx.AsyncClient(
        headers={"Authorization": f"Bearer {mint_token(user_id, name)}"},
        transport=_transport,
        timeout=60,
    )
    client = None
    try:
        client = await ClientFactory(
            ClientConfig(httpx_client=http, streaming=False)
        ).create_from_url(settings.finance_agent_url.rstrip("/"))
        message = Message(
            role=Role.ROLE_USER,
            message_id=uuid.uuid4().hex,
            task_id=task_id or None,
            context_id=context_id or None,
            parts=[
                new_text_part(text or skill),
                new_data_part({"skill": skill, **payload}),
            ],
        )
        last = None
        async for event in client.send_message(SendMessageRequest(message=message)):
            last = event
    except Exception as e:  # network errors, A2A protocol errors, a card we can't use
        raise FinanceUnavailable(f"{type(e).__name__}: {e}") from e
    finally:
        if client is not None:
            await client.close()
        await http.aclose()
    if last is None:
        raise FinanceUnavailable("empty response")
    if last.HasField("task"):
        task = last.task
        msg = task.status.message
        data = next((d for d in get_data_parts(msg.parts) if isinstance(d, dict)), {})
        return Reply(
            STATES.get(task.status.state, "working"),
            get_message_text(msg),
            data,
            task.id,
            task.context_id,
        )
    if last.HasField("message"):
        msg = last.message
        data = next((d for d in get_data_parts(msg.parts) if isinstance(d, dict)), {})
        return Reply(
            "completed", get_message_text(msg), data, msg.task_id, msg.context_id
        )
    raise FinanceUnavailable("unexpected response")
