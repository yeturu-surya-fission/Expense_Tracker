"""FastMCP server application.

Exposes the Starlette app for uvicorn. Lifespan is wired into the
FastMCP instance in mcp_server/tools/__init__.py.

Run with:  python -m mcp_server.server
"""

import logging

from mcp_server.tools import mcp as fastmcp_app

log = logging.getLogger("mcp_server.server")

# Import tool modules so their @mcp.tool decorators register on the FastMCP instance.
from importlib import import_module

for _module in ("conversation", "expenses", "budgets", "reports", "friends", "splits", "bills", "groups"):
    import_module(f"mcp_server.tools.{_module}")

# Starlette app for uvicorn. Lifespan is already wired into the
# FastMCP instance in mcp_server/tools/__init__.py.
app = fastmcp_app.http_app()

__all__ = ["app"]
