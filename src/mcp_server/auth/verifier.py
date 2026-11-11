""" the gate : verifies backend issued jwts none= 401 no mcp processing"""

import jwt
from fastmcp.server.auth import AccessToken, TokenVerifier
from pydantic import AnyHttpUrl
from mcp_server.auth.keys import get_public_key,TOKEN_ALG

TOKEN_ISSUER="expense-tracker-backend"
TOKEN_AUDIENCE="expense-tracker-mcp"
REQUIRED_CLAIMS=["sub","iss","aud","iat","nbf","exp","chat_type"]
VALID_CHAT_TYPES={"private","group"}

def _claims_from(claims:dict) -> dict|None:
    """coerce the jwt claims into exactly what tools need or none if anything is off"""
    user_id=claims.get("sub")
    chat_type=claims.get("chat_type")
    if user_id is None or chat_type not in VALID_CHAT_TYPES:
        return None
    out:dict={"user_id": int(user_id),"chat_type":chat_type}
    if chat_type=="group" and claims.get("group_chat_id") is not None:
        out["group_chat_id"]= int(claims["group_chat_id"])
    return out

class MCPVerifier(TokenVerifier):
    def __init__(self):
        super().__init__(required_scopes=[],base_url=AnyHttpUrl("http://localhost:9000"))

    async def verify_token(self,token:str):
        try:
            claims=jwt.decode(
                token,
                get_public_key(),
                algorithms=[TOKEN_ALG],
                issuer=TOKEN_ISSUER,
                audience=TOKEN_AUDIENCE,
                options={"require":REQUIRED_CLAIMS}
                )
        except jwt.PyJWTError:
            return None
        tool_claims=_claims_from(claims)
        if tool_claims is None:
            return None
        return AccessToken(token=token,client_id=f"user:{tool_claims['user_id']}",scopes=[], claims=tool_claims)
        
