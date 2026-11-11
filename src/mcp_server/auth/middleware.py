""" maps verified claims onto ctx state - the per-request bridge tools read"""

from fastmcp.server.middleware import Middleware,MiddlewareContext

from mcp.server.auth.middleware.auth_context import get_access_token

class ClaimsToState(Middleware):
    async def on_call_tool(self,ctx:MiddlewareContext,call_next):
        at=get_access_token()
        claims=getattr(at,"claims",None) or {}
        mcp_ctx=ctx.fastmcp_context
        if mcp_ctx is not None and claims.get("user_id") is not None:
            # set_state/get_state are ASYNC in fastmcp 4.x — the awaits are mandatory
            await mcp_ctx.set_state("user_id", claims["user_id"])
            await mcp_ctx.set_state("chat_type", claims.get("chat_type", "private"))
            await mcp_ctx.set_state("group_chat_id", claims.get("group_chat_id"))
        return await call_next(ctx)

    