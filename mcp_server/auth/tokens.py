"""
Issuing and checking access tokens (JWT, RS256).

Two kinds, both bound to this server (`aud`) and short-lived:
  service  sub 'client:<id>'   scope 'mcp platform'   10 min   a first-party client proving who it is (client_credentials)
  session  sub 'user:<id>'     scope 'mcp user'       30 min   one person, in one chat context; `act` names the client acting for them
"""
import time
import uuid
from datetime import datetime, timezone

import jwt
from sqlalchemy import select

from mcp_server.auth.keys import get_keys
from mcp_server.config import settings
from mcp_server.db.models import RevokedToken
from mcp_server.db.session import session_scope

LEEWAY_SECONDS = 5


class TokenError(Exception):
    pass


def _issue(claims: dict, ttl_seconds: int) -> tuple[str, dict]:
    now = int(time.time())
    body = {
        "iss": settings.issuer, "aud": settings.resource_url, "iat": now, "nbf": now, "exp": now + ttl_seconds,
        "jti": uuid.uuid4().hex, **claims,
    }
    keys = get_keys().current
    return jwt.encode(body, keys.private_pem, algorithm="RS256", headers={"kid": keys.kid, "typ": "at+jwt"}), body


def issue_service_token(client_id: str) -> tuple[str, int]:
    ttl = settings.service_token_ttl_minutes * 60
    token, _ = _issue({"sub": f"client:{client_id}", "scope": "mcp platform", "typ": "service", "client_id": client_id}, ttl)
    return token, ttl


def issue_session_token(user_id: int, client_id: str, chat_type: str = "private", group_chat_id: int | None = None) -> tuple[str, int, dict]:
    ttl = settings.session_token_ttl_minutes * 60
    claims = {
        "sub": f"user:{user_id}", "uid": user_id, "scope": "mcp user", "typ": "session", "client_id": client_id,
        "act": {"sub": f"client:{client_id}"}, "chat_type": chat_type,
    }
    if group_chat_id is not None:
        claims["group_chat_id"] = group_chat_id
    token, body = _issue(claims, ttl)
    return token, ttl, body


def decode(token: str) -> dict:
    """Signature, issuer, audience, expiry and token type are all checked here. Raises TokenError."""
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256":
            raise TokenError("unsupported algorithm")
        key = get_keys().public_keys.get(header.get("kid", ""))
        if key is None:
            raise TokenError("unknown signing key")
        return jwt.decode(
            token, key, algorithms=["RS256"], audience=settings.resource_url, issuer=settings.issuer,
            leeway=LEEWAY_SECONDS, options={"require": ["exp", "iat", "iss", "aud", "sub", "jti"]},
        )
    except jwt.PyJWTError as e:
        raise TokenError(str(e)) from e


async def is_revoked(jti: str) -> bool:
    async with session_scope() as s:
        return (await s.execute(select(RevokedToken.jti).where(RevokedToken.jti == jti))).first() is not None


async def revoke(jti: str, exp: int) -> None:
    async with session_scope() as s:
        if (await s.get(RevokedToken, jti)) is None:
            s.add(RevokedToken(jti=jti, expires_at=datetime.fromtimestamp(exp, tz=timezone.utc)))
