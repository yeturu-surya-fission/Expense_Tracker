"""The real 401 gate over real HTTP: the deployment path, not a shortcut."""
import time
import uuid

import jwt
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from backend.auth.keys import get_private_key
from mcp_server.auth.keys import TOKEN_ALG

TOKEN_ISSUER = "expense-tracker-backend"
TOKEN_AUDIENCE = "expense-tracker-mcp"


def _client(url: str, token: str | None) -> Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return Client(StreamableHttpTransport(url=url, headers=headers))


async def test_valid_token_calls_a_tool(mcp_url):
    from backend.auth.issuer import issue_mcp_token
    async with _client(mcp_url, issue_mcp_token(user_id=1, chat_type="private")) as client:
        result = await client.call_tool("respond_to_user", {"kind": "help"})
        assert "expense tracker" in result.content[0].text.lower()


async def test_expired_token_is_401(mcp_url):
    now = int(time.time())
    token = jwt.encode({"iss": TOKEN_ISSUER, "sub": "1", "aud": TOKEN_AUDIENCE, "iat": now - 700,
                        "nbf": now - 700, "exp": now - 10, "jti": uuid.uuid4().hex,
                        "chat_type": "private"},
                       get_private_key(), algorithm=TOKEN_ALG)
    with pytest.raises(Exception):
        async with _client(mcp_url, token):
            pass


async def test_tampered_token_is_401(mcp_url):
    from backend.auth.issuer import issue_mcp_token
    token = issue_mcp_token(user_id=1, chat_type="private")
    head, body, sig = token.split(".")
    with pytest.raises(Exception):
        async with _client(mcp_url, f"{head}.{body[:-2]}xx.{sig}"):
            pass


async def test_wrong_audience_is_401(mcp_url):
    now = int(time.time())
    token = jwt.encode({"iss": TOKEN_ISSUER, "sub": "1", "aud": "other-service", "iat": now,
                        "nbf": now, "exp": now + 600, "jti": uuid.uuid4().hex,
                        "chat_type": "private"},
                       get_private_key(), algorithm=TOKEN_ALG)
    with pytest.raises(Exception):
        async with _client(mcp_url, token):
            pass


async def test_missing_token_is_401(mcp_url):
    with pytest.raises(Exception):
        async with _client(mcp_url, None):
            pass


async def test_garbage_token_is_401(mcp_url):
    with pytest.raises(Exception):
        async with _client(mcp_url, "not.a.jwt"):
            pass
