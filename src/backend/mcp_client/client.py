"""app/mcp_client/client.py one backend run = one mcp session with one fresh token.
nothing is cached or shared between messages the only credentials that exists for any single message
is the token minted for that message, and it dies with it"""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from backend.auth.issuer import issue_mcp_token
from backend.config import settings

@asynccontextmanager
async def mcp_session(
    *,user_id:int,chat_type:str,group_chat_id:int|None=None
)-> AsyncIterator[Client]:
    token=issue_mcp_token(user_id=user_id,chat_type=chat_type,group_chat_id=group_chat_id)
    transport=StreamableHttpTransport(
        url=settings.mcp_server_url,
        headers={"Authorization":f"Bearer {token}"},
    )
    async with Client(transport) as client:
        yield client
        