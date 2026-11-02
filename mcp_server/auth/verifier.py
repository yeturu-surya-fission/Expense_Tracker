"""The MCP SDK calls this for EVERY request: there is no session to remember, so a token is checked every time."""
from mcp.server.auth.provider import AccessToken

from mcp_server.auth import tokens
from mcp_server.config import settings


class JwtVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = tokens.decode(token)
        except tokens.TokenError:
            return None
        if await tokens.is_revoked(claims["jti"]):
            return None
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id", "")),
            scopes=str(claims.get("scope", "")).split(),
            expires_at=int(claims["exp"]),
            resource=settings.resource_url,
            subject=str(claims["sub"]),
            claims=claims,
        )
