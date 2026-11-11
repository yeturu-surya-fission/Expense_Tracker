"""FastMCP instance — tool registration entry point.

All MCP tools are registered here. Individual tool modules import `mcp`
from this package and decorate their functions with `@mcp.tool`.

Lifespan is wired here so database initialization happens on server
startup and cleanup on shutdown.
"""

import logging
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from starlette.responses import JSONResponse

from mcp_server.database.session import get_engine, get_init_db

log = logging.getLogger("mcp_server.tools")


@asynccontextmanager
async def lifespan(app):
    """Application lifespan: init database on startup, dispose on shutdown."""
    log.info("MCP server: initializing database...")
    init_db = get_init_db()
    await init_db()
    log.info("MCP server: database initialized")
    yield
    log.info("MCP server: shutting down...")
    engine = get_engine()
    await engine.dispose()
    log.info("MCP server: shutdown complete")


mcp = FastMCP("Expense Tracker MCP", lifespan=lifespan)

from mcp_server.auth.middleware import ClaimsToState
from mcp_server.auth.verifier import MCPVerifier

mcp.auth=MCPVerifier()
mcp.add_middleware(ClaimsToState())


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    return JSONResponse({"status": "ok", "server": "expense-tracker-mcp"})

__all__ = ["mcp"]
