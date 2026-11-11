"""MCP server entry point.

Runs the FastMCP server with Streamable HTTP transport on port 9000.

Usage:
    python -m mcp_server.server        # direct run
    uvicorn mcp_server.server:app      # uvicorn run
"""

import uvicorn

from mcp_server.server import app

if __name__ == "__main__":
    uvicorn.run(
        "mcp_server.server:app",
        host="0.0.0.0",
        port=9000,
        log_level="info",
    )
