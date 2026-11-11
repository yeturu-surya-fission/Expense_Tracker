
import uuid
from datetime import UTC, datetime, timedelta

import jwt

from backend.auth.keys import get_private_key

TOKEN_ALGORITHM="EdDSA"
TOKEN_ISSUER="expense-tracker-backend"
TOKEN_AUDIENCE="expense-tracker-mcp"
TOKEN_TTL=timedelta(minutes=10)

def issue_mcp_token(*, user_id:int, chat_type:str,group_chat_id:int |None=None)-> str:
    now=datetime.now(UTC)
    claims={
        "iss": TOKEN_ISSUER,
        "sub": str(user_id),
        "aud": TOKEN_AUDIENCE,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + TOKEN_TTL).timestamp()),
        "jti": uuid.uuid4().hex,
        "chat_type": chat_type,
        **({"group_chat_id": group_chat_id} if group_chat_id is not None else {}),
    }
    return jwt.encode(claims, get_private_key(), algorithm=TOKEN_ALGORITHM)

