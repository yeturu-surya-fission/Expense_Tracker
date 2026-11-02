"""The MCP server: tools + resources + auth, served over Streamable HTTP at <public_url>/mcp."""
import asyncio
import logging
from contextlib import asynccontextmanager

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from mcp_server import tools as _tools  # noqa: F401  (importing registers every tool)
from mcp_server.auth.routes import register_auth_routes
from mcp_server.auth.verifier import JwtVerifier
from mcp_server.config import settings
from mcp_server.db.session import engine, init_db
from mcp_server.tools.registry import SPECS, register_all

log = logging.getLogger("mcp_server")

INSTRUCTIONS = (
    "Expense tracker: personal expenses, budgets, reports, friends, bill splitting and group settle-ups. "
    "Every tool returns {status, text, data, input_requests}. If status is 'input_required', ask the human the "
    "questions in input_requests and call the SAME tool again with the same arguments plus answers={key: option id}. "
    "Nothing is saved until all questions are answered."
)


class ScopedMCPServer(MCPServer):
    """tools/list shows each caller only the tools it could actually call (the gate still enforces it on every call)."""

    async def list_tools(self):
        tools = await super().list_tools()
        token = get_access_token()
        if token is None:
            return []
        scopes, chat = set(token.scopes or []), (token.claims or {}).get("chat_type", "private")
        if "platform" in scopes:  # a platform client (the bot) builds tool definitions for the people it serves
            return tools
        return [t for t in tools if t.name in SPECS and SPECS[t.name].scope in scopes and chat in SPECS[t.name].contexts]


def create_mcp() -> MCPServer:
    mcp = ScopedMCPServer(
        name="expense-tracker",
        title="Expense Tracker",
        instructions=INSTRUCTIONS,
        token_verifier=JwtVerifier(),
        auth=AuthSettings(
            issuer_url=settings.issuer,
            resource_server_url=settings.resource_url,
            required_scopes=["mcp"],
            validate_token_resource=True,
        ),
    )
    register_all(mcp)
    register_auth_routes(mcp)
    return mcp


def create_app():
    """The ASGI application (uvicorn mcp_server.main:app)."""
    mcp = create_mcp()
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=settings.max_request_bytes,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=settings.hosts, allowed_origins=sorted(settings.origins),
        ),
    )
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a):
        await init_db()
        background: list[asyncio.Task] = []
        if settings.reminders_enabled:
            from mcp_server.jobs import reminder_loop
            background.append(asyncio.create_task(reminder_loop()))
        try:
            async with inner(a):
                yield
        finally:
            for t in background:
                t.cancel()
            await engine.dispose()

    app.router.lifespan_context = lifespan
    app.state.mcp = mcp
    return app
