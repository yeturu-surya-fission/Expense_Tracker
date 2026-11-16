"""
Who may call us. The expense tracker signs a short-lived token (RS256 JWT) for every A2A call and publishes its public
keys; we check signature, issuer, audience (that it was meant for US), expiry and scope. No shared secret travels.

The token says who the employee is (`sub` = 'user:<id>', `name`); we trust that because only the tracker can sign it.
"""
import time
from typing import Awaitable, Callable

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm
from starlette.requests import Request
from starlette.responses import JSONResponse

from finance_agent.config import settings

JwksProvider = Callable[[], Awaitable[dict]]
CACHE_SECONDS = 300
SCOPE = "a2a"


async def fetch_jwks() -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(settings.trusted_jwks_url)
        r.raise_for_status()
        return r.json()


class TokenVerifier:
    def __init__(self, provider: JwksProvider = fetch_jwks):
        self._provider = provider
        self._keys: dict[str, object] = {}
        self._fetched = 0.0

    async def _refresh(self) -> None:
        jwks = await self._provider()
        self._keys = {k["kid"]: RSAAlgorithm.from_jwk(k) for k in jwks.get("keys", []) if k.get("kty") == "RSA" and k.get("kid")}
        self._fetched = time.monotonic()

    async def _key(self, kid: str):
        if kid not in self._keys or time.monotonic() - self._fetched > CACHE_SECONDS:
            await self._refresh()
        return self._keys.get(kid)

    async def verify(self, token: str) -> dict | None:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256":
                return None
            key = await self._key(header.get("kid", ""))
            if key is None:
                return None
            claims = jwt.decode(
                token, key, algorithms=["RS256"], audience=settings.audience, issuer=settings.trusted_issuer.rstrip("/"),
                leeway=5, options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except (jwt.PyJWTError, httpx.HTTPError, ValueError, KeyError):
            return None
        if SCOPE not in str(claims.get("scope", "")).split() or not str(claims.get("sub", "")).startswith("user:"):
            return None
        return claims


def authenticate_middleware(verifier: TokenVerifier, protected_prefix: str = "/a2a"):
    """HTTP middleware: only the A2A endpoint needs a token (the agent card is public by design)."""
    async def middleware(request: Request, call_next):
        if request.url.path.startswith(protected_prefix):
            header = request.headers.get("authorization", "")
            claims = await verifier.verify(header[7:].strip()) if header.lower().startswith("bearer ") else None
            if claims is None:
                return JSONResponse({"error": "invalid_token"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            request.state.claims = claims
        return await call_next(request)
    return middleware
