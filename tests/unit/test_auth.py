"""The auth spine: token claims on the signing side, accept/reject on the verifying side."""
import time
import uuid

import jwt
import pytest

from backend.auth.issuer import (
    TOKEN_ALGORITHM,
    TOKEN_AUDIENCE,
    TOKEN_ISSUER,
    TOKEN_TTL,
    issue_mcp_token,
)
from backend.auth.keys import get_private_key
from mcp_server.auth.keys import TOKEN_ALG, get_public_key
from mcp_server.auth.verifier import MCPVerifier, REQUIRED_CLAIMS


def _decode(token: str) -> dict:
    return jwt.decode(token, get_public_key(), algorithms=[TOKEN_ALG],
                      issuer=TOKEN_ISSUER, audience=TOKEN_AUDIENCE)


def _verifier() -> MCPVerifier:
    return MCPVerifier()


def test_private_token_claims():
    claims = _decode(issue_mcp_token(user_id=42, chat_type="private"))
    assert claims["sub"] == "42"
    assert claims["iss"] == TOKEN_ISSUER
    assert claims["aud"] == TOKEN_AUDIENCE
    assert claims["chat_type"] == "private"
    assert "group_chat_id" not in claims
    assert claims["exp"] - claims["iat"] == int(TOKEN_TTL.total_seconds()) == 600
    assert claims["nbf"] <= claims["exp"]


def test_group_token_carries_group_chat_id():
    claims = _decode(issue_mcp_token(user_id=7, chat_type="group", group_chat_id=-100123))
    assert claims["chat_type"] == "group"
    assert claims["group_chat_id"] == -100123


def test_jti_unique_per_token():
    a = _decode(issue_mcp_token(user_id=1, chat_type="private"))["jti"]
    b = _decode(issue_mcp_token(user_id=1, chat_type="private"))["jti"]
    assert a != b


async def test_verify_private():
    access = await _verifier().verify_token(issue_mcp_token(user_id=7, chat_type="private"))
    assert access is not None
    assert access.claims == {"user_id": 7, "chat_type": "private"}
    assert access.client_id == "user:7"
    assert access.scopes == []


async def test_verify_group_coerces_types():
    access = await _verifier().verify_token(issue_mcp_token(user_id="9", chat_type="group", group_chat_id=-5))
    assert access.claims == {"user_id": 9, "chat_type": "group", "group_chat_id": -5}


def _mint(**overrides) -> str:
    now = int(time.time())
    claims = {"iss": TOKEN_ISSUER, "sub": "5", "aud": TOKEN_AUDIENCE, "iat": now, "nbf": now,
              "exp": now + 600, "jti": uuid.uuid4().hex, "chat_type": "private", **overrides}
    return jwt.encode(claims, get_private_key(), algorithm=TOKEN_ALGORITHM)


async def test_reject_tampered_token():
    token = issue_mcp_token(user_id=5, chat_type="private")
    head, body, sig = token.split(".")
    assert await _verifier().verify_token(f"{head}.{body[:-2]}xx.{sig}") is None


async def test_reject_expired():
    assert await _verifier().verify_token(_mint(exp=int(time.time()) - 10, iat=int(time.time()) - 700)) is None


async def test_reject_wrong_audience():
    assert await _verifier().verify_token(_mint(aud="someone-else")) is None


async def test_reject_missing_required_claim():
    for claim in REQUIRED_CLAIMS:
        now = int(time.time())
        partial = {"iss": TOKEN_ISSUER, "sub": "5", "aud": TOKEN_AUDIENCE, "iat": now, "nbf": now,
                   "exp": now + 600, "jti": uuid.uuid4().hex, "chat_type": "private"}
        partial.pop(claim)
        token = jwt.encode(partial, get_private_key(), algorithm=TOKEN_ALGORITHM)
        assert await _verifier().verify_token(token) is None, f"missing {claim} was accepted"


async def test_reject_bad_chat_type():
    assert await _verifier().verify_token(_mint(chat_type="channel")) is None


def test_alg_mismatch_rejected():
    with pytest.raises(Exception):
        _decode(jwt.encode({"sub": "5"}, get_private_key(), algorithm="HS256"))
